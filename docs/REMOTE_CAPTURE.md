# Remote capture (record live HLS on the pipeline host)

Kaggle cannot pull the CCTV origin reliably. Measured on 2026-11-09:

| Source | Download | Needed | Result |
| --- | --- | --- | --- |
| Kaggle to `pub2.hk-opt.com` | ~71 kbps | ~600 kbps | 6x slower than real time |
| Pipeline host to `pub2.hk-opt.com` | ~1.4 Mbps | ~600 kbps | 2x faster than real time |
| Kaggle to GitHub (control) | 8.4 MB/s | - | Kaggle egress is fine |

When Kaggle records directly, segments expire before they finish downloading and
ffmpeg concatenates truncated chunks (`Packet corrupt`, `bytestream -30`).

## Flow

```text
Kaggle --POST /capture--> pipeline host --ffmpeg--> data/captures/cap_*.mp4
Kaggle <--GET /capture/<signed token>-- pipeline host
Kaggle: process_video -> replay -> Nextcloud clips
```

The host records with `ffmpeg -live_start_index 0`, stream copy, `+faststart`, no
audio. It subtracts the playlist backlog from the wall clock so `started_at` is the
real WIB time of the first frame.

## Latency (events first, evidence later)

Measured on 2026-11-11 with 10 live windows (`live_20261111_105429`, daytime
traffic, 124-184 events/window):

| Stage | Old (2026-11-10) | New |
| --- | --- | --- |
| capture + processing + evidence | 61 s | 70 s |
| waiting for media (secrets + queue) | 273 s | 43 s |
| Nextcloud upload | 25 s | 45 s |
| replay + ingest | 10 s | 7 s |
| **average delivery lag** | **368 s** | **167 s** |

The new lag is flat per window instead of growing; the remaining upload cost no
longer gates delivery.

What changed:

- Events, tracks, and the run record are replayed as soon as `process_video`
  returns (`media_manifest=None`). Evidence encoding and the Nextcloud upload
  happen afterwards in the `PostQueue` media workers.
- Clips are attached later through `POST /webhook/media` on the pipeline
  receiver. The payload is one `media` list per run; `fact_media` /
  `fact_media_event` upsert, so retries are safe. Nothing waits for it.
- `PostQueue` now has two independent ordered queues: replay (immediate) and
  media (evidence -> upload -> patch). `drain()` waits for both.
- `annotate_media=True` renders evidence clips/snapshots with all lane polygons
  plus the bounding box of the vehicle that caused the event. Other vehicles are
  left unboxed. A per-frame `frames.jsonl` log is written during processing and
  used for the boxes; clips are encoded with OpenCV and transcoded to H.264.
- Evidence is deferred only when `upload_media=True`; without it the run still
  generates evidence inline.
- Nextcloud config is still resolved per upload. All optional secret names
  (`NEXTCLOUD_TIMEOUT`, `NEXTCLOUD_RETRIES`, `NEXTCLOUD_MAX_SIMPLE_UPLOAD_MB`,
  `NEXTCLOUD_VERIFY_TLS`, `NEXTCLOUD_UPLOAD_WORKERS`) must exist in Kaggle
  Secrets; a missing secret makes `get_secret` fail slowly and used to add
  ~80-100 s per window.

```python
from lane_analytics.capture import fetch_capture
from lane_analytics.colab import PostQueue, activate

queue = PostQueue(upload_workers=2)
capture = fetch_capture(
    "https://lane-api.syaifudin-fam.my.id",
    token,
    seconds=30,
    url=STREAM_URL,
)
activate(
    source="file",
    video_path=capture.path,
    base_timestamp=capture.started_at,
    upload_media=True,
    annotate_media=True,
    replay=True,
    post_queue=queue,   # events posted immediately; media follows in the background
)
queue.drain()           # at the end of the cell
```

A media failure is recorded on the item (`media_error`) and never affects the
events that were already delivered.

## Usage

```python
from lane_analytics.colab import activate

activate(
    source="server_capture",
    capture_seconds=30,
    capture_url="https://pub2.hk-opt.com/LiveApp/streams/<id>.m3u8",
    # token from PIPELINE_WEBHOOK_TOKEN
    replay=True,
)
```

The production notebook uses this by default:

```python
SOURCE_MODE = "live"
LIVE_SOURCE = "server"     # or "kaggle"
CAPTURE_BASE_URL = "https://lane-api.syaifudin-fam.my.id"
```

Set `LIVE_SOURCE = "kaggle"` to fall back to the old local recorder.

Manual smoke test (writes one MP4, no processing):

```powershell
$env:PIPELINE_WEBHOOK_TOKEN = "..."   # do not commit
lane-analytics capture --seconds 30 --out output/captures
```

## Server contract

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| `POST` | `/capture` | `Authorization: Bearer <webhook token>` | Body `{"url": ..., "seconds": 5-60}`; blocks for one window; `409` if busy |
| `GET` | `/capture/<token>` | signed URL, TTL 900 s | Returns the MP4 |

Server settings (all optional):

| Variable | Default |
| --- | --- |
| `CAPTURE_ENABLED` | `true` |
| `CAPTURE_ALLOWED_HOSTS` | `pub2.hk-opt.com` |
| `CAPTURE_PUBLIC_BASE` | `https://lane-api.syaifudin-fam.my.id/capture` |
| `CAPTURE_DIR` | `<data>/captures` |
| `CAPTURE_URL_TTL_SECONDS` | `900` |
| `CAPTURE_RETENTION_SECONDS` | `3600` |
| `CAPTURE_MAX_FILES` | `50` |

The download token is signed with `MEDIA_PROXY_SECRET` and carries
`kind=capture`, so it cannot be reused against `/media/<token>`.

## Kaggle smoke test (one cell)

Add `PIPELINE_WEBHOOK_TOKEN` to Kaggle Secrets, then paste this into a cell with
Internet enabled. It records 20 s on the host, times the POST, downloads the MP4,
checks the size, and prints the measured throughput.

```python
import time
import requests
from kaggle_secrets import UserSecretsClient

BASE = "https://lane-api.syaifudin-fam.my.id"
URL = "https://pub2.hk-opt.com/LiveApp/streams/501264524429471982550467.m3u8"
TOKEN = UserSecretsClient().get_secret("PIPELINE_WEBHOOK_TOKEN")

t0 = time.time()
r = requests.post(f"{BASE}/capture", json={"url": URL, "seconds": 20},
                  headers={"Authorization": f"Bearer {TOKEN}"}, timeout=180)
r.raise_for_status()
meta = r.json()
print("recorded in", round(time.time() - t0, 1), "s ->", meta["duration_s"], "s,",
      round(meta["size_bytes"] / 1e6, 2), "MB, started", meta["started_at"])

t0 = time.time()
with requests.get(meta["download_url"], stream=True, timeout=180) as resp:
    resp.raise_for_status()
    data = resp.content
elapsed = time.time() - t0
assert len(data) == meta["size_bytes"], (len(data), meta["size_bytes"])
print("downloaded", round(len(data) / 1e6, 2), "MB in", round(elapsed, 1), "s ->",
      round(len(data) / 1e6 / elapsed, 2), "MB/s")
```

Measured from a Windows client on 2026-11-09: 15 s window -> 9.8 s POST, 1.76 MB,
1.5 s download (~1.2 MB/s). A concurrent request returns `409`.

## Deployment note

`/opt/lane-pipeline` is not a git clone. The receiver runs from
`WorkingDirectory=/opt/lane-pipeline` with `/opt/lane-pipeline/.venv`. Deploy by
copying `src/lane_pipeline/capture.py`, `src/lane_pipeline/config.py`, and
`src/lane_pipeline/receiver/app.py`, then `systemctl restart lane-receiver`.
Captures land in `PIPELINE_DATA_DIR/captures` (`/var/lib/lane-pipeline/captures`).

