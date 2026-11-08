# Data Schema

This document defines the event contract, the per-track summary file, and the report
artifacts. The machine-readable event schema lives at
[`schema/lane_event.schema.json`](schema/lane_event.schema.json).

## Event stream (JSONL)

Each processed event is appended to `output/events/<run_id>.jsonl`:

- UTF-8, one JSON object per line, no wrapping array.
- Events appear in processing order (frame index ascending, with the order described
  below when several events share a frame).
- Each batch is flushed immediately, so a partial file remains valid JSONL.
- An empty file means no lane events were produced.

### Event types

| `event_type` | Emitted when | Key fields |
| --- | --- | --- |
| `lane_enter` | A stable lane assignment starts. | `lane_name`, `lane_index`, `lane_role` |
| `lane_exit` | A lane stint closes (change, stale track, or video end). | `lane_name`, `dwell_seconds` |
| `lane_change` | A vehicle moves from one lane to another. | `previous_lane`, `lane_name`, `dwell_seconds` |
| `lane_hogging_indication` | A configured rule threshold is met. | `rule_name`, `severity`, `dwell_seconds`, `message` |

For a lane change, the order is `lane_exit` → `lane_change` → `lane_enter`, all at the
same frame/timestamp.

### Example: lane change

```json
{"camera_id":"camera_01","timestamp":"2026-11-07T08:00:05","track_id":2,"object_class":"car","confidence":0.945,"event_type":"lane_exit","lane_name":"lane_center","lane_index":2,"lane_role":"normal","previous_lane":"","dwell_seconds":2.6,"class_id":2,"frame_index":56,"video_time_s":5.6,"severity":"","rule_name":"","message":"","schema_version":"1.0","event_id":"7c25c2fbd15a462ea2de766b6fd2281e"}
{"camera_id":"camera_01","timestamp":"2026-11-07T08:00:05","track_id":2,"object_class":"car","confidence":0.945,"event_type":"lane_change","lane_name":"lane_right","lane_index":3,"lane_role":"overtaking","previous_lane":"lane_center","dwell_seconds":2.6,"class_id":2,"frame_index":56,"video_time_s":5.6,"severity":"","rule_name":"","message":"","schema_version":"1.0","event_id":"71da270938bd456593dad083d812a9d8"}
{"camera_id":"camera_01","timestamp":"2026-11-07T08:00:05","track_id":2,"object_class":"car","confidence":0.945,"event_type":"lane_enter","lane_name":"lane_right","lane_index":3,"lane_role":"overtaking","previous_lane":"lane_center","dwell_seconds":0.0,"class_id":2,"frame_index":56,"video_time_s":5.6,"severity":"","rule_name":"","message":"","schema_version":"1.0","event_id":"2d0da69c00b843e99ad941f2b9bbbaf9"}
```

### Example: hogging indication

```json
{"camera_id":"camera_01","timestamp":"2026-11-07T08:00:12","track_id":12,"object_class":"truck","confidence":0.88,"event_type":"lane_hogging_indication","lane_name":"lane_right","lane_index":3,"lane_role":"overtaking","previous_lane":"","dwell_seconds":4.2,"class_id":7,"frame_index":120,"video_time_s":12.0,"severity":"high","rule_name":"heavy_vehicle_in_overtaking_lane","message":"Truk/bus menggunakan lajur overtaking melebihi batas waktu.","schema_version":"1.0","event_id":"73db63605edc4a948e2c89581fbb6525"}
```

### Fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `camera_id` | string | yes | Logical camera identifier from config. |
| `timestamp` | string | yes | Naive local ISO-8601: `YYYY-MM-DDTHH:MM:SS`. Derived from `base_timestamp + frame_index / source_fps`. |
| `track_id` | integer | yes | ByteTrack track identifier, unique within a run. |
| `object_class` | string | yes | `car`, `bus`, or `truck` (config-dependent). |
| `confidence` | number | yes | Detection confidence at the event frame, `0.0 - 1.0`. |
| `event_type` | string | yes | One of the four event types above. |
| `lane_name` | string | yes | Lane involved in the event (new lane for enter/change/hogging, old lane for exit). |
| `lane_index` | integer or null | no | Configured lane index, scoped per direction (`lane_name` is the unique key). |
| `lane_role` | string | no | Configured role, e.g. `normal`, `overtaking`. |
| `previous_lane` | string | no | Old lane for `lane_change` and `lane_enter` after a change. |
| `dwell_seconds` | number | no | Continuous time in the lane at the event. For `lane_exit`/`lane_change`, the old stint duration. |
| `class_id` | integer | no | COCO class id (`2` car, `5` bus, `7` truck). |
| `frame_index` | integer | no | Source frame index (not processed-frame index). |
| `video_time_s` | number | no | Source video time: `frame_index / source_fps`. |
| `severity` | string | no | `low`, `medium`, `high`, or empty. Set for hogging indications. |
| `rule_name` | string | no | Configured rule name. Set for hogging indications. |
| `message` | string | no | Human-readable rule description. |
| `schema_version` | string | no | Event contract version: `1.0` (legacy) or `1.1` when session fields are present. |
| `session_id` | string | no | Groups the 30-second windows of one live click or one long-video batch. Empty for legacy runs. |
| `window_index` | integer or null | no | 0-based window number inside the session. |
| `window_count` | integer or null | no | Total planned windows in the session. |
| `event_id` | string | no | UUID4 hex, 32 characters. Use for downstream deduplication. |

The first seven fields plus `lane_name` form the stable contract. New optional fields may
be added in future versions; consumers should ignore unknown keys and check
`schema_version` if strict.

### Sessions and windows

A **run** is one processed video file (`run_id`). A **session** is one user action: one
live-stream click or one long-video batch. When a session is split into 30-second
windows:

```text
session_id  = live_20261108_082400
run_id      = live_20261108_082400_w0000   # window_index 0
run_id      = live_20261108_082400_w0001   # window_index 1
window_count = 3
```

- `run_id` is unique per window and remains the supersede key.
- `session_id` is shared by all windows of the session.
- `track_id` resets per window. Do not sum distinct `track_id` values across windows as
  unique vehicles; label session totals as window-scoped.
- Legacy `1.0` runs have no session fields; treat them as a one-window session if
  grouping is needed (`session_id = run_id`, `window_index = 0`, `window_count = 1`).

### Semantics and caveats

- `lane_enter` and `lane_exit` are generated only if enabled in `occupancy`.
- `dwell_seconds` is wall-clock source time, not processed-frame count. Frame stride does
  not change it.
- A stale track is finalized at its last observation time. A vehicle that left the frame
  does not accrue dwell until the end of the video.
- A rule indication means "the configured condition was met". It is not proof of a
  traffic violation.
- `track_id` is run-scoped. Use `event_id` as the unique event key and group vehicles by
  `(camera_id, track_id)` only within a run.

## Track summaries (JSONL)

`output/tracks/<run_id>.jsonl` contains one object per completed track:

```json
{
  "camera_id": "camera_01",
  "track_id": 3,
  "object_class": "truck",
  "class_id": 7,
  "first_frame": 0,
  "last_frame": 111,
  "first_timestamp": "2026-11-07T08:00:00",
  "last_timestamp": "2026-11-07T08:00:11",
  "first_video_time_s": 0.0,
  "last_video_time_s": 11.1,
  "lane_change_count": 0,
  "hogging_event_count": 1,
  "dominant_lane": "lane_right",
  "total_lane_dwell_seconds": 9.1,
  "session_id": "",
  "window_index": null,
  "window_count": null,
  "lane_stints": [
    {
      "stint_id": 1,
      "lane_name": "lane_right",
      "lane_index": 3,
      "lane_role": "overtaking",
      "enter_frame": 1,
      "enter_time_s": 0.1,
      "enter_timestamp": "2026-11-07T08:00:00",
      "exit_frame": 92,
      "exit_time_s": 9.2,
      "exit_timestamp": "2026-11-07T08:00:09",
      "dwell_seconds": 9.1
    }
  ]
}
```

`lane_stints` is the per-vehicle lane history. `dominant_lane` is the lane with the
largest total dwell. `session_id`, `window_index`, and `window_count` mirror the event
fields and are empty/`null` for legacy runs.

## CSV exports

| File | One row per | Key columns |
| --- | --- | --- |
| `events.csv` | Event | All event fields. |
| `vehicles.csv` | Track | Track metadata, dominant lane, total dwell, counts. |
| `lane_stints.csv` | Lane stint | Track, lane, enter/exit, dwell, `hogging_indication`. |
| `lane_usage.csv` | Lane × class | Vehicles, stints, total/avg/max dwell, shares. |
| `lane_changes.csv` | `lane_change` event | Old/new lane, class, dwell. |
| `hogging_events.csv` | Hogging event | Rule, severity, dwell, message. |
| `event_intervals.csv` | Time bucket | Event count and breakdown. |

`lane_usage.csv` columns:

| Column | Description |
| --- | --- |
| `lane_name`, `lane_index`, `lane_role` | Lane identity. |
| `object_class` | Vehicle class. |
| `vehicle_count` | Distinct tracks observed in the lane/class. |
| `stint_count` | Number of lane stints (a vehicle can enter twice). |
| `total_dwell_s`, `avg_dwell_s`, `max_dwell_s` | Occupancy duration statistics. |
| `vehicle_share_pct` | Share of all unique vehicles. |
| `dwell_share_pct` | Share of total lane dwell. |

## Summary JSON

`summary.json` is the machine-readable run report:

```json
{
  "schema_version": "1.0",
  "camera_id": "camera_01",
  "generated_at": "2026-11-07T23:23:03",
  "time_range": {"start": "2026-11-07T08:00:00", "end": "2026-11-07T08:00:29"},
  "interval_seconds": 60,
  "total_vehicles": 6,
  "total_events": 20,
  "vehicles_by_class": {"car": 4, "bus": 1, "truck": 1},
  "events_by_type": {
    "lane_enter": 8,
    "lane_exit": 8,
    "lane_change": 2,
    "lane_hogging_indication": 2
  },
  "events_by_lane": {"lane_right": 8, "lane_left": 5},
  "lane_usage": [],
  "lane_changes": {"total": 2, "by_class": {"car": 2}, "pairs": []},
  "hogging": {
    "total": 2,
    "by_class": {"truck": 1, "car": 1},
    "by_severity": {"high": 1, "medium": 1},
    "by_rule": {"heavy_vehicle_in_overtaking_lane": 1},
    "by_lane": {"lane_right": 2},
    "events": []
  },
  "intervals": [],
  "peak_interval": null,
  "run": {}
}
```

*Abridged example: `lane_usage`, `hogging.events`, and `intervals` are truncated, and
`run` is omitted.*

| Top-level key | Description |
| --- | --- |
| `total_vehicles` | Number of completed track summaries. |
| `total_events` | Number of structured events. |
| `vehicles_by_class` | Unique vehicles per class. |
| `events_by_type` | Event count per type. |
| `events_by_lane` | Event count per lane. |
| `lane_usage` | Lane × class usage rows (same columns as `lane_usage.csv`). |
| `lane_changes` | Total, by class, and most frequent lane pairs. |
| `hogging` | Total, breakdowns, and compact event list. |
| `intervals` | Event timeseries by bucket. |
| `peak_interval` | Busiest interval, or `null`. |
| `run` | Run metadata (source, detector, frame counts, FPS, lanes, rules). |

For session-aware runs, `run.session_id`, `run.window_index`, and `run.window_count` are
also present. A session groups multiple 30-second `run_id` windows; see
[HANDOFF_SESSION_WINDOWS.md](./HANDOFF_SESSION_WINDOWS.md).

## Downstream pipeline expectations

A pipeline such as **Real-Time Video Analytics Data Pipeline** can:

1. Read events JSONL line by line and validate with the JSON Schema.
2. Deduplicate on `event_id`.
3. Partition by `camera_id` and event date.
4. Join events with `tracks/<run_id>.jsonl` on `(camera_id, track_id)` when per-vehicle
   context is needed.
5. Aggregate lane occupancy, lane changes, and hogging indications by class, lane, rule,
   severity, and time bucket.

Recommended handling:

- Parse `timestamp` as local naive time and attach the camera timezone from a reference
  table, or derive UTC at ingestion.
- Do not assume lane names, indices, roles, or rule names are fixed enums; they are
  configuration-driven.
- Treat `track_id` as run-scoped.
- Expect additional optional fields in future `schema_version` releases.
