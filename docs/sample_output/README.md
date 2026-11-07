# Sample output

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
