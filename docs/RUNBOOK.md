# Runbook

## Google Colab (recommended)

### 1. Open the notebook

Upload or clone this repository, then open
`notebooks/colab_lane_occupancy.ipynb`.

Colab already provides CUDA-enabled PyTorch. Do **not** install a CPU torch wheel there.

### 2. Install

```python
!pip install -q ultralytics
!pip install -q -e . --no-deps
!lane-analytics doctor
```

### 3. Mount Drive and select a video

```python
from google.colab import drive
drive.mount("/content/drive")
VIDEO_PATH = "/content/drive/MyDrive/traffic-sample.mp4"
```

Alternatively, upload a file with `google.colab.files.upload()` or record a live stream
(section 4).

### 4. Optional: record a public toll-road CCTV stream

The notebook includes HLS playlists for the Semarang-Solo toll road. They are live 24/7
streams, not downloadable videos. `lane-analytics record` captures fixed-length chunks,
reconnects when the playlist rotates, and remuxes the result:

```python
from lane_analytics.recorder import record_stream

result = record_stream(
    "https://jmlive.jasamarga.com/hls/15/c59b1716-ea0a-43ff-b9b0-d26bd863a428/index.m3u8",
    "/content/cctv_km486_500.mp4",
    duration=300,
    chunk_seconds=30,
)
print(result.actual_seconds, result.complete)
```

Use this responsibly: only the currently broadcasting window is available, and repeated
aggressive requests should be avoided.

### 5. Use the shipped camera profile (or calibrate a new one)

`config/config.yaml` ships a profile calibrated for the Tj. Priok KM 58+400 (B) camera
(1024x768, night, 18 fps): six lanes across both carriageways (three per direction), with
the median-side lane marked as `overtaking` in each direction. The polygons were drawn on a
1434x1076 capture (same 4:3 aspect) and normalized, so they apply to the 1024x768 video.

For a different camera, calibrate against one frame:

```powershell
lane-analytics calibrate --video data/raw/clip.mp4 --frame 0 --out output/calibration/grid.jpg
```

The image contains the current polygons, a pixel grid every 50 px, and a normalized `y`
ruler on the right edge. For each lane edge:

1. Read its pixel `x` at two heights (e.g. `y=400` and `y=700`).
2. Divide `x` by the frame width and `y` by the frame height.
3. Write the polygon into `config/config.yaml` (`lanes[].polygon`).
4. Re-run `calibrate` until the edges sit on the painted lines.

The notebook's configuration cell only rewrites the video path and device; it leaves the
calibrated polygons alone.

### 6. Preview and validate

```powershell
lane-analytics preview-lanes --video data/raw/clip.mp4 --frame 2160
```

Open `output/lane_preview.jpg`. Each polygon should cover one lane over the part of the
road where vehicles will be analyzed. Leave a small gap at painted dividers.

Per-camera checks:

- Do the polygons cover the road, not the shoulder or median?
- Does the anchor stay inside the correct lane while a vehicle passes? The anchor is drawn
  as a small white dot in each box; for wide near-field boxes it should slide toward the
  outer bottom corner (left half of the frame to the left, right half to the right).
- Are lane `role` values consistent with the hogging rules?
- If traffic is reversed, update `direction` labels (they are informational).
- How long is the observation window? Divide the zone length by typical speed and make
  sure the hogging thresholds fit inside it.

> **This camera is short-baseline.** The six polygons cover only the visible stretch in each
> direction, so a vehicle stays inside them for roughly 0.5-3 s at highway speed. The
> shipped thresholds (0.4 s heavy vehicle, 2.0 s car) are tuned for that window. A camera
> with a longer view needs proportionally larger thresholds.


### 7. Run

GPU:

```python
!lane-analytics analyze --config config/config.yaml --device 0
```

CPU:

```python
!lane-analytics analyze --config config/config.yaml --device cpu --frame-stride 3 --imgsz 480
```

Quick smoke test:

```python
!lane-analytics analyze --config config/config.yaml --max-frames 300 --frame-stride 3 --no-video
```

### 8. Review outputs

- `output/videos/<run_id>_annotated.mp4`: browser playback needs H.264. The notebook
  re-encodes with ffmpeg.
- `output/reports/<run_id>/summary.md`: human report.
- `output/reports/<run_id>/hogging_events.csv`: rule indications.
- `output/reports/<run_id>/lane_usage.csv`: lane × class occupancy.
- `output/reports/<run_id>/lane_stints.csv`: every lane stint with dwell duration.
- `output/events/<run_id>.jsonl`: full event stream.
- `output/tracks/<run_id>.jsonl`: per-vehicle lane history.

### 9. Save results

Colab deletes `/content` when the runtime ends. Copy `output/` to Drive:

```python
import shutil
from datetime import datetime
from pathlib import Path

destination = (
    Path("/content/drive/MyDrive/lane-occupancy-output")
    / datetime.now().strftime("%Y%m%d_%H%M%S")
)
shutil.copytree("output", destination, dirs_exist_ok=True)
```

## Local CPU run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -e .
lane-analytics doctor
lane-analytics analyze --video data/raw/traffic.mp4 --frame-stride 3 --imgsz 480
```

### Optional parallel segmented run

`analyze` is single-pass by default (`--workers 1`). To opt into segmented workers for a
long clip:

```powershell
lane-analytics analyze --video data/raw/tj_priok_5min.mp4 `
  --workers 3 --segment-seconds 12 --overlap-seconds 3
```

Defaults are three workers, 12 s segments, and a 3 s overlap (starts every 9 s). The
final IDs, events, and reports are continuous; the local-to-canonical map is in
`output/tracks/<run_id>_id_handovers.jsonl`.

Use `--workers 1` when a single YOLO pass already saturates the CPU, or on a single GPU.
Check the log line `Parallel: N segments finished in ... (... of segment work)`: if
segment work is much larger than the sequential run and wall time is worse, the machine
has no spare compute for parallelism.

On a multi-GPU host (for example Kaggle `GPU T4 x2`), pin one worker to each device:

```powershell
lane-analytics analyze --video data/raw/tj_priok_5min.mp4 `
  --workers 2 --devices 0,1
```

Each worker loads its own model on its own GPU; the final merge, annotated video, and
reports still run once. Do not pass the same device twice.

Compare a small window before committing:

```powershell
lane-analytics analyze --max-frames 1200 --workers 1 --no-video --no-evidence
lane-analytics analyze --max-frames 1200 --workers 3 --no-video --no-evidence
```

## Offline demo

No YOLO or PyTorch required:

```powershell
pip install -r requirements-demo.txt
pip install -e . --no-deps
lane-analytics demo --output-dir output/demo
```

## Rebuild reports from existing JSONL

```powershell
lane-analytics summarize output/events/run.jsonl `
  --tracks output/tracks/run.jsonl `
  --out output/reports/rebuilt
```

Without `--tracks`, event aggregates are still produced but lane-usage tables are empty.

## Troubleshooting

### No vehicles detected

- Confirm the video opens and has the expected classes.
- Check `lane-analytics doctor`; `ultralytics` and `torch` should be installed for real
  footage.
- Lower `model.conf` (e.g. `0.25`) or increase `model.imgsz`.
- The default classes are COCO `2` (car), `5` (bus), `7` (truck). Add `3` for motorcycles
  if needed.

### Vehicles appear but all labels say `lane n/a`

- The anchor is outside every polygon. Run `preview-lanes` and compare.
- For perspective views, keep `bottom_center`; for top-down views, try `center`.
- Expand the polygon slightly, but keep the divider gap.

### Too many lane changes

- Increase `occupancy.min_lane_frames` to 3-5.
- Check for overlapping polygons at dividers.
- Raise `model.conf` to reduce noisy boxes.

### No hogging events

- `hogging.enabled` must be `true`.
- Rule `lanes`/`lane_roles` must match `lanes[].name`/`lanes[].role` exactly.
- The vehicle may not have reached `min_dwell_seconds`.
- Check `lane_stints.csv` for the actual dwell durations.

### Too many hogging events

- Raise `min_dwell_seconds`.
- Confirm lane roles reflect the real road; a `normal` lane should not carry an
  `overtaking` rule.
- Consider disabling rules during congestion or roadworks.

### Annotated video does not play in the browser

OpenCV writes `mp4v`, which many browsers reject. Re-encode:

```bash
ffmpeg -y -i input.mp4 -vcodec libx264 -pix_fmt yuv420p -movflags +faststart output.mp4
```

The Colab notebook does this automatically.

### Slow processing

- Increase `video.frame_stride` (2-4).
- Lower `model.imgsz` (480 or 416).
- Use `--no-video` if only data is needed.
- On Colab, switch to a GPU runtime and `--device 0`.
- Video decoding and OpenCV annotation still use CPU; annotation can be the bottleneck.

### `ffmpeg/ffprobe not found`

Install ffmpeg or run the recording step in Colab, which ships it:

```bash
apt-get install -y ffmpeg
```

### Colab disconnects during a long run

Process a shorter clip, use `--max-frames`, or run the recording step in chunks and copy
outputs to Drive before the session ends.
