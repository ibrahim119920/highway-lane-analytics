# Highway Lane Occupancy & Hogging Report

- **Camera:** `demo_camera_01`
- **Generated:** 2026-11-07T23:23:03
- **Period:** 2026-11-07T08:00:00 to 2026-11-07T08:00:15
- **Unique vehicles:** **6**
- **Structured events:** **20**
- **Lane changes:** **2**
- **Lane-hogging indications:** **2**
- **Source video:** `output\demo_smoke\input\demo_input.mp4`
- **Frames processed:** 200 (stride 1, 136.49 fps processing)

## Vehicles by class

| Class | Vehicles | Share |
| --- | --- | --- |
| car | 4 | 66.7% |
| bus | 1 | 16.7% |
| truck | 1 | 16.7% |

## Lane usage by class

| Lane | Index | Role | Class | Vehicles | Stints | Total dwell | Avg dwell | Dwell share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| lane_left | 1 | normal | car | 2 | 2 | 10.9s | 5.5s | 24.3% |
| lane_center | 2 | normal | bus | 1 | 1 | 8.4s | 8.4s | 18.8% |
| lane_center | 2 | normal | car | 2 | 2 | 4.5s | 2.2s | 10.0% |
| lane_right | 3 | overtaking | car | 2 | 2 | 11.9s | 6.0s | 26.6% |
| lane_right | 3 | overtaking | truck | 1 | 1 | 9.1s | 9.1s | 20.3% |

## Lane changes

| Class | Lane changes |
| --- | --- |
| car | 2 |

Most frequent transitions:

| From | To | Count |
| --- | --- | --- |
| lane_center | lane_right | 1 |
| lane_left | lane_center | 1 |

## Lane-hogging indications

| Timestamp | Track | Class | Lane | Dwell | Severity | Rule |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-07T08:00:02 | 3 | truck | lane_right | 2.0s | high | heavy_vehicle_in_overtaking_lane |
| 2026-11-07T08:00:13 | 6 | car | lane_right | 6.0s | medium | prolonged_overtaking_lane |

## Events by type

| Event type | Count |
| --- | --- |
| lane_enter | 8 |
| lane_exit | 8 |
| lane_change | 2 |
| lane_hogging_indication | 2 |

## Run metadata

| Key | Value |
| --- | --- |
| base_timestamp | 2026-11-07T08:00:00 |
| detector | synthetic-lane-manifest |
| duration_s | 20.0 |
| frame_stride | 1 |
| frames_processed | 200 |
| frames_read | 200 |
| hogging_rules | ['heavy_vehicle_in_overtaking_lane', 'prolonged_overtaking_lane'] |
| lanes | [{'name': 'lane_left', 'index': 1, 'role': 'normal', 'direction': 'southbound', 'polygon': [[0.12, 0.03], [0.36, 0.03], [0.36, 0.97], [0.12, 0.97]]}, {'name': 'lane_center', 'index': 2, 'role': 'normal', 'direction': 'southbound', 'polygon': [[0.365, 0.03], [0.615, 0.03], [0.615, 0.97], [0.365, 0.97]]}, {'name': 'lane_right', 'index': 3, 'role': 'overtaking', 'direction': 'southbound', 'polygon': [[0.62, 0.03], [0.88, 0.03], [0.88, 0.97], [0.62, 0.97]]}] |
| output_fps | 10.0 |
| processing_fps | 136.49 |
| processing_seconds | 1.465 |
| run_id | demo_run |
| source_fps | 10.0 |
| source_video | output\demo_smoke\input\demo_input.mp4 |

> Lane-hogging is a rule-based **indication** produced from class, lane, and
> dwell time. It is not a legal determination. Review the configuration against
> the actual road rules before using the output for enforcement.
