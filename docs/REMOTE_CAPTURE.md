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
