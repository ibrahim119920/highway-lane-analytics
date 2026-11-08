# Sample output

## Real camera - Tj. Priok KM 58+400 B

| File | Description |
| --- | --- |
| `km58_lane_mapping.jpg` | Six-lane calibration drawn from the CCTV point of view (coordinates in 1434x1076 space). |
| `km58_lane_preview.jpg` | The same polygons rendered by `lane-analytics preview-lanes` on the 1024x768 video. |
| `km58_summary.md` | Report from the full 5-minute run (219 vehicles, 8 hogging indications). |
| `km58_hogging_events.csv` | Rule-based indications from that run. |
| `km58_lane_usage.csv` | Lane x class occupancy from that run. |

The images document the calibrated lane geometry; the CSV/Markdown files are real
inference output from the sample clip.

## Synthetic demo

These artifacts were produced by the offline synthetic demo (no YOLO inference):

```powershell
lane-analytics demo --output-dir output/demo_smoke --duration 20 --fps 10 --width 640 --height 640
```

The synthetic three-lane scene intentionally includes:

- two lane changes by cars,
- a truck that stays in the overtaking lane,
- a car that stays in the overtaking lane long enough to trigger the medium rule.

| File | Description |
| --- | --- |
| `preview.jpg` | Annotated frame with lane polygons, vehicle labels, dwell time, HUD. |
| `demo_annotated.mp4` | Full annotated demo video. |
| `demo_events.jsonl` | Structured event stream. |
| `demo_tracks.jsonl` | Per-vehicle lane history and dwell summaries. |
| `summary.md` | Markdown report. |
| `lane_usage.csv` | Lane × class occupancy table. |
| `hogging_events.csv` | Rule-based hogging indications. |

The synthetic backend is for pipeline testing only. Real footage uses YOLO + ByteTrack;
the event and report contracts are identical.
