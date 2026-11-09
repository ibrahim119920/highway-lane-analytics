# Evidence clips in Nextcloud (WebDAV)

The pipeline webhook carries **JSON only** (events, tracks, run metadata). Short
evidence clips stay on Nextcloud and are referenced by relative paths in the event
payload. This keeps the receiver small and lets the dashboard/portfolio stream video
from storage.

## What gets uploaded

`lane_analytics.evidence` already writes, for every run:

```text
output/evidence/<run_id>/
  index.json
  clips/clip_<frame>.mp4          # 8 s, centred on the event
  snapshots/snap_<frame>.jpg
```

The uploader reads `index.json`, keeps only `lane_hogging_indication` by default, and
uploads one clip per unique clip file (lane-change groups share a clip). The remote
layout is deterministic:

```text
TrafficAnalytics/events/<camera_id>/<YYYY>/<MM>/<DD>/<run_id>/
  clips/clip_<frame>.mp4
  snapshots/snap_<frame>.jpg
```

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `NEXTCLOUD_URL` | `https://cloud.syaifudin-fam.my.id` | Nextcloud base URL |
| `NEXTCLOUD_USER` | — | dedicated upload user |
| `NEXTCLOUD_APP_PASSWORD` | — | App Password, not the main password |
| `NEXTCLOUD_ROOT` | `TrafficAnalytics` | root folder for this project |
| `NEXTCLOUD_TIMEOUT` | `60` | seconds per WebDAV request |
| `NEXTCLOUD_RETRIES` | `4` | retries for 408/425/429/5xx/52x |
| `NEXTCLOUD_MAX_SIMPLE_UPLOAD_MB` | `90` | guard before a single PUT |
| `NEXTCLOUD_VERIFY_TLS` | `true` | keep true on the public host |

In Colab/Kaggle use Secrets; do not commit the App Password. `NextcloudConfig.from_env()`
reads the four main variables from the environment first, then from Kaggle Secrets
(`kaggle_secrets.UserSecretsClient`) and Colab Secrets (`google.colab.userdata`), so the
notebook does not need to copy them into `os.environ` itself.

## CLI

Upload a run's hogging clips and write a manifest:

```powershell
$env:NEXTCLOUD_URL = "https://cloud.syaifudin-fam.my.id"
$env:NEXTCLOUD_USER = "lane-upload"
$env:NEXTCLOUD_APP_PASSWORD = "<app password>"

lane-analytics media upload `
  --index output/evidence/<run_id>/index.json `
  --camera-id tj_priok_km58_400b `
  --out output/evidence/<run_id>/media_manifest.json
```

Dry-run (build manifest only, no HTTP):

```powershell
lane-analytics media upload --index ... --dry-run
```

Then merge the media fields into the events during replay:

```powershell
lane-analytics replay `
  --events output/events/<run_id>.jsonl `
  --tracks output/tracks/<run_id>.jsonl `
  --summary output/reports/<run_id>/summary.json `
  --run-config output/reports/<run_id>/run_config.json `
  --media-manifest output/evidence/<run_id>/media_manifest.json `
  --stats
```

## Python / notebook

```python
from lane_analytics.media import upload_evidence

result = upload_evidence(
    "output/evidence/<run_id>/index.json",
    camera_id="tj_priok_km58_400b",
    event_types=("lane_hogging_indication",),
)
print(result.as_dict())
media_by_event = result.by_event_id()
```

`activate(..., upload_media=True)` does the same automatically after
`process_video` and passes the manifest to `replay_run`.

## Event fields

Only events with a successfully uploaded clip get these fields:

```json
{
  "media_id": "6308ae17e390904c",
  "video_path": "TrafficAnalytics/events/.../clips/clip_000060.mp4",
  "thumbnail_path": "TrafficAnalytics/events/.../snapshots/snap_000060.jpg",
  "media_status": "COMPLETED",
  "file_size": 2191169,
  "clip_duration_s": 8.0,
  "clip_start_s": 2.687,
  "clip_end_s": 10.687
}
```

Status values:

| Status | Meaning |
| --- | --- |
| `COMPLETED` | clip + thumbnail uploaded |
| `PARTIAL` | clip uploaded, thumbnail failed |
| `SKIPPED` | remote clip already has the same size |
| `FAILED` | clip upload failed; no `video_path` is attached |
| `PLANNED` | dry-run only |

The pipeline schema (`fact_media`) and the signed media proxy/dashboard are separate
phases; this module only prepares and uploads the files and the event fields.

## Upload behaviour

- `MKCOL` creates parent folders; `405` means the folder already exists.
- `PUT` is idempotent: the same remote path is overwritten.
- `HEAD` checks the remote size before upload (skip when equal) **and after every
  PUT**. `COMPLETED` is only reported when the stored `Content-Length` matches the
  local file, so a truncated upload is not advertised to the pipeline.
- A clip that uploads but has no thumbnail (or a failed thumbnail) becomes `PARTIAL`.
  `FAILED` entries never contribute `video_path` to an event.
- Retries: `408`, `425`, `429`, `5xx`, `52x`, timeouts.
- Clips are short (about 1-5 MB). A single PUT is used; a file over the configured
  guard is rejected with a clear message instead of silently hitting a PHP/Cloudflare
  body limit. Chunked upload can be added later if longer clips are needed.
- Upload failures never abort the vision run; the event is still sent without
  `video_path`.

## Clip encoding

Evidence clips are written as H.264 / `yuv420p` with `+faststart` so the browser can
stream them with Range requests. The encoder resolution order is: system `ffmpeg`,
then the bundled binary from `imageio-ffmpeg`, then OpenCV. The OpenCV fallback writes
MPEG-4 Part 2, which Chrome/Edge cannot decode; keep ffmpeg or `imageio-ffmpeg`
installed (it is in `requirements.txt`) if the player stays black.

## Verified end-to-end (2026-11-09)

A real run was uploaded from a Windows machine through Cloudflare to the production
Nextcloud, then replayed to `lane-api`:

- `run_id=km58_final`, camera `tj_priok_km58_400b`, 8 `lane_hogging_indication` clips.
- Every PUT was followed by a matching `HEAD` (`200`, sizes 1.4-2.0 MB).
- Nextcloud `Range: bytes=0-1023` returned `206` with `Content-Range`.
- Replay: 412 accepted items (192 events, 219 tracks, 1 run), 0 duplicates, 8 events
  with media fields.
- Pipeline: 8 `fact_media` rows, 8 `fact_media_event` links, and
  `silver/fact_media/**` + `silver/fact_media_event.parquet` exported.
- Dashboard: Hogging signals → 8 rows with **Open clip**; signed proxy returned
  `200`/`206`, bad signature and expired token `403`, missing file `404`.
- The clips are H.264; opening the signed URL loaded video metadata (1024x768,
  7.92 s) and the browser requested `206` ranges.

## Security

- Dedicated Nextcloud user with access only to `TrafficAnalytics/`.
- App Password, not the account password; store it in Secrets.
- The browser never talks to Nextcloud directly. The dashboard/portfolio uses a
  server-side signed proxy; the App Password stays on the server.
- Do not commit `.env`, tokens, or screenshots containing credentials.
