# Architecture

## Purpose

The pipeline turns a highway video into three related artifacts:

1. an annotated video showing what the system understood,
2. a stream of structured lane events,
3. per-track occupancy summaries and aggregate reports.

It is a prototype, not an enforcement system. Lane assignment and hogging indications are
heuristic and depend on correctly calibrated lane polygons.

## Pipeline

```mermaid
flowchart TD
    A[VideoCapture] --> B{frame_index % stride == 0?}
    B -- no --> A
    B -- yes --> C[Detector.update]
    C --> D[ByteTrack / manifest]
    D --> E[LaneTracker.update]
    E --> F[events JSONL]
    E --> G[Annotator.draw]
    G --> H[VideoWriter]
    A -. end .-> I[LaneTracker.finalize]
    I --> F
    I --> J[tracks JSONL]
    F --> K[summarize_events]
    J --> K
    K --> L[CSV / JSON / Markdown]
```

### 1. Frame sampling

`video.frame_stride` controls how many source frames are skipped. The default is 1
(every frame): with an ~18 fps source, skipping frames can make ByteTrack fail to
confirm a fast near-field vehicle, because an unconfirmed track is associated against
its last position and the IoU can drop below the 0.3 match threshold. Timing is always
computed from the source frame index (`frame_index / source_fps`), so a stride of 2 or 3
does not change dwell durations - only how early a fast vehicle is confirmed.

### 2. Detection and tracking

`YoloByteTrackDetector` wraps Ultralytics `model.track(..., persist=True)` with
`bytetrack.yaml`. It filters to the configured COCO classes (default `car`, `bus`,
`truck`) and returns `TrackedObject` observations with a run-scoped `track_id`.

The offline demo uses `ManifestDetector`, which replays a per-frame JSONL manifest through
the same interface. Analytics and annotation therefore exercise the same code path.

### 3. Coordinate spaces

| Space | Where used | Meaning |
| --- | --- | --- |
| Normalized `(x, y)` | Config lanes, rule metadata | `0.0 - 1.0` relative to frame width/height. Resolution independent. |
| Pixel `(x, y)` | Detection boxes, annotation | Absolute frame coordinates. |
| Source time `s` | Dwell, events | `frame_index / source_fps`. |

The lane tracker converts the chosen bbox anchor to normalized coordinates before testing
polygons. This keeps one configuration valid across 720p, 1080p, and 4K sources.

### 4. Lane assignment

For each detection:

1. Choose an anchor point from the bounding box:
   `center`, `bottom_center` (default), `bottom_left`, `bottom_right`, or `top_center`.
2. Optionally apply the **adaptive anchor**: when `anchor_shift_enabled` is on, a wide
   near-field box slides from the bottom-centre toward its outer bottom corner
   (bottom-left when the box centre is in the left half of the frame, bottom-right in
   the right half). The slide is linear between `anchor_shift_small_bbox_px` and
   `anchor_shift_large_bbox_px`.
3. Normalize the anchor by frame width/height.
4. Test it against every lane polygon with ray casting.
5. If more than one polygon contains the point, choose the smallest polygon by area.

`bottom_center` is usually best for perspective highway cameras because the tyres touch
the road surface, while the bbox centre can drift over a lane divider. `center` is fine
for straight top-down views. For high, oblique cameras where near-field boxes are wider
than a lane, enable the adaptive anchor.

The annotator draws the anchor as a small white dot inside each box, which makes lane
assignment errors easy to spot in the output video.

Lane-name labels are lifted into the sky, a fixed fraction of the frame height above the
horizon end of each polygon and staggered by lane index (northbound labels grow leftward,
southbound rightward), with a thin leader line down to the lane. They never sit over the
roadway or a vehicle. Object rendering uses two passes: every box first, then every label.
Vehicle label backgrounds are translucent, so a label that overlaps another box dims it
instead of hiding it.


### 5. Hysteresis

Raw point tests can flicker when a vehicle straddles a divider. The tracker therefore
keeps a **pending lane** and only commits a change after `min_lane_frames` consecutive
observations. If the vehicle returns to its current lane before the transition becomes
stable, the pending transition is cancelled and no event is emitted.

The committed stint start time is the first observation of the new lane, not the
confirmation frame. This avoids losing the time spent transitioning.

### 6. Stint lifecycle

Each track has at most one open `LaneStint`:

```text
lane_enter  ->  open stint (enter_frame, enter_time, enter_timestamp)
            ->  dwell = current_time - enter_time
lane_change ->  close old stint, open new stint, emit exit + change + enter
lane_exit   ->  close stint, record exit_time and dwell_seconds
```

Open stints are closed when:

- the vehicle changes lane,
- the track is stale for `max_track_age_frames`,
- the video ends (`finalize`).

Closing at the **last observation time** avoids inflating dwell for a vehicle that left
the frame long before the video ended.

### 7. Event ordering

For a lane change, events are emitted in this order:

```text
lane_exit (old lane, old dwell)
lane_change (from old to new, old dwell)
lane_enter (new lane, dwell 0)
```

All four event types share one JSONL stream and one schema.

### 8. Hogging evaluation

For each stable observation, rules are matched against the current lane and vehicle class.
A rule fires when:

- the lane matches by name or role,
- the class matches (empty class list means all classes),
- the vehicle is not already visibly transitioning to another lane,
- `dwell >= rule.min_dwell_seconds`,
- the `(stint, rule)` pair has not fired, or `retrigger_after_seconds` has elapsed.

`retrigger_after_seconds: 0` means one indication per track/lane stint/rule. A positive
value allows repeated indications during a long stint.

The tracker also exposes the active rule and severity to the annotator, so the box turns
red and the HUD shows `! HOGGING`.

### 9. Finalization and reports

At the end of the run:

- all open stints are closed,
- one `TrackSummary` per track is written to `tracks/<run_id>.jsonl`,
- `summarize_events` builds lane usage, lane-change, hogging, and interval aggregates,
- `write_reports` writes CSV, JSON, and Markdown artifacts.

If `evidence.enabled` is true, `evidence.py` then cuts a snapshot and an 8-second clip
(4 s before + 4 s after) around every event. Media is cut from the annotated video, so the
snapshot and clip show the box, lane label, and hogging badge. Events that share a frame
(`lane_exit` → `lane_change` → `lane_enter`) share one snapshot and one clip. The mapping is
written to `output/evidence/<run_id>/index.json`. For runs started with `--start-frame`, the
event's source time is converted to the annotated video's local time using
`start_frame / source_fps`.

## Module map

| Module | Responsibility |
| --- | --- |
| `config.py` | Typed YAML loading, lane/rule validation, run snapshot. |
| `geometry.py` | Point-in-polygon, area, centroid, lane assignment. |
| `detector.py` | YOLO + ByteTrack adapter. |
| `lane_tracker.py` | Per-track state machine for lane stints and events. |
| `hogging.py` | Rule model and severity matching. |
| `annotator.py` | Lane polygons, boxes, labels, HUD. |
| `analytics.py` | Event/track aggregation and report writing. |
| `pipeline.py` | Frame loop and artifact orchestration. |
| `evidence.py` | Snapshot + 8-second clip evidence per event. |
| `demo.py` | Synthetic scene and manifest detector. |
| `recorder.py` | Chunked HLS/HTTP recording with ffmpeg. |
| `replay.py` | Paced webhook sender for precomputed events/tracks/run metadata. |
| `colab.py` | One-call Colab activation: Drive zip or live stream, then replay. |
| `cli.py` | Command-line entry points. |

## Extension points

- **Homography / perspective calibration**: replace the point-in-polygon lane mapper with
  a bird's-eye-view projection for more accurate lane geometry and speed.
- **Speed estimation**: use anchor displacement per source second in a calibrated plane.
- **Multi-camera identity**: use an external re-identification layer; `track_id` is only
  run-scoped.
- **Webhook replay sink**: `replay.py` posts batch artifacts as paced micro-batches to a local
  webhook. The receiver, Kafka, pipeline, and dashboard live in the separate pipeline project;
  this repository only sends. See `docs/HANDOFF_DATA_PIPELINE_DASHBOARD.md`.
- **Rule engine**: add relative-speed or vehicle-length conditions to `HoggingRule`.

## Known failure modes

| Symptom | Likely cause | Mitigation |
| --- | --- | --- |
| `lane n/a` labels | Anchor outside every polygon | Expand polygons, check anchor, preview lanes. |
| Too many lane changes | Polygons overlap or boundary flicker | Increase `min_lane_frames`, add a divider gap. |
| No hogging events | Rule role/lane names mismatch | Check `lanes[].role` and `lane_roles`, lower threshold. |
| Hogging on normal usage | Threshold too low or wrong role | Review road rules and raise the threshold. |
| Short dwell durations | Track ID switched or vehicle occluded | Improve detection size/confidence, review footage. |
| First box ~0.5-1 s late on a fast near-field vehicle | `frame_stride > 1`; ByteTrack matches unconfirmed tracks against their last position and can miss across large motion | Keep `frame_stride: 1`, or lower it for latency-sensitive runs. |
| Slow CPU run | Full-resolution YOLO on every frame | Use stride 2-3, `imgsz: 480`, `--no-video`. |
