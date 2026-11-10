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

## Throughput (30 s window in <30 s)

Measured on 2026-11-10, a 30 s window took 254 s. The cost was not bandwidth: the
18 evidence files were only 5.0 MB, but every file re-issued one `MKCOL` per path
level plus two `HEAD`s, ~10 serial WebDAV requests per file over Cloudflare.

| Stage | Before | After |
| --- | --- | --- |
| capture + download | 17 s | 17 s, overlapped with processing (prefetch) |
| inference (540 frames) | 9.1 s | 9.1 s |
| evidence (9 clips + 9 snapshots) | 10 s | ~4 s (4 workers) |
| Nextcloud upload (18 files) | 210 s | ~15-20 s, in the background |
| replay | 6-11 s | in the background, ordered |
| **window wall** | **254 s** | **~17-20 s** |

What changed:

- `NextcloudUploader` caches collections it has created, so each path level gets
  one `MKCOL` instead of one per file; upload workers get their own
  `requests.Session`.
- `upload_evidence(..., workers=4)` uploads files in parallel (`PUT` + size `HEAD`
  per file stays sequential inside a worker). Override with
  `NEXTCLOUD_UPLOAD_WORKERS`.
- `generate_evidence(..., clip_workers=4)` writes snapshots and encodes clips in
  parallel. Pass `1` for the old sequential behaviour.
- `PostQueue` moves upload + replay off the capture path. Uploads of consecutive
  windows run in parallel; a single replay worker keeps webhook delivery ordered.
  Call `drain()` before the notebook cell ends.
- The notebook prefetches window N+1 with `fetch_capture` while window N is
  processed, then calls `activate(source="file", video_path=...)`.

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
    replay=True,
    post_queue=queue,   # returns immediately; upload + replay run in the background
)
queue.drain()           # at the end of the cell
```

An upload failure is recorded on the item (`media_error`) and the window is still
replayed without media fields; it never aborts the run.

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

