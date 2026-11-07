# Highway Lane Occupancy & Hogging Report

- **Camera:** `tj_priok_km58_400b`
- **Generated:** 2026-11-08T02:35:22
- **Period:** 2026-11-07T23:15:38 to 2026-11-07T23:20:40
- **Unique vehicles:** **219**
- **Structured events:** **192**
- **Lane changes:** **12**
- **Lane-hogging indications:** **8**
- **Source video:** `data\raw\km58_400B_tj_priok_20261107_161545.mp4`
- **Frames processed:** 2728 (stride 2, 8.1 fps processing)

## Vehicles by class

| Class | Vehicles | Share |
| --- | --- | --- |
| car | 163 | 74.4% |
| truck | 43 | 19.6% |
| bus | 13 | 5.9% |

## Lane usage by class

| Lane | Index | Role | Class | Vehicles | Stints | Total dwell | Avg dwell | Dwell share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| nb_lane_1 | 1 | normal | bus | 1 | 1 | 4.2s | 4.2s | 3.0% |
| nb_lane_1 | 1 | normal | truck | 9 | 9 | 30.2s | 3.4s | 21.6% |
| sb_lane_1 | 1 | normal | bus | 3 | 3 | 6.3s | 2.1s | 4.5% |
| sb_lane_1 | 1 | normal | car | 7 | 7 | 7.9s | 1.1s | 5.6% |
| sb_lane_1 | 1 | normal | truck | 3 | 3 | 2.3s | 0.8s | 1.7% |
| nb_lane_2 | 2 | normal | bus | 3 | 3 | 0.6s | 0.2s | 0.4% |
| nb_lane_2 | 2 | normal | car | 6 | 6 | 12.4s | 2.1s | 8.9% |
| nb_lane_2 | 2 | normal | truck | 12 | 14 | 27.0s | 1.9s | 19.3% |
| sb_lane_2 | 2 | normal | bus | 2 | 2 | 1.1s | 0.6s | 0.8% |
| sb_lane_2 | 2 | normal | car | 7 | 7 | 12.3s | 1.8s | 8.8% |
| sb_lane_2 | 2 | normal | truck | 5 | 5 | 4.9s | 1.0s | 3.5% |
| nb_lane_3 | 3 | overtaking | car | 9 | 9 | 18.2s | 2.0s | 13.0% |
| nb_lane_3 | 3 | overtaking | truck | 3 | 3 | 1.0s | 0.3s | 0.7% |
| sb_lane_3 | 3 | overtaking | bus | 2 | 2 | 0.4s | 0.2s | 0.3% |
| sb_lane_3 | 3 | overtaking | car | 10 | 10 | 10.3s | 1.0s | 7.4% |
| sb_lane_3 | 3 | overtaking | truck | 2 | 2 | 0.8s | 0.4s | 0.6% |

## Lane changes

| Class | Lane changes |
| --- | --- |
| truck | 9 |
| bus | 2 |
| car | 1 |

Most frequent transitions:

| From | To | Count |
| --- | --- | --- |
| sb_lane_1 | sb_lane_2 | 4 |
| nb_lane_3 | nb_lane_2 | 3 |
| nb_lane_2 | nb_lane_3 | 2 |
| sb_lane_2 | sb_lane_3 | 2 |
| nb_lane_2 | nb_lane_1 | 1 |

## Lane-hogging indications

| Timestamp | Track | Class | Lane | Dwell | Severity | Rule |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-07T23:15:46 | 53 | car | nb_lane_3 | 2.1s | medium | prolonged_overtaking_lane |
| 2026-11-07T23:16:15 | 129 | car | nb_lane_3 | 2.1s | medium | prolonged_overtaking_lane |
| 2026-11-07T23:16:47 | 292 | car | nb_lane_3 | 2.1s | medium | prolonged_overtaking_lane |
| 2026-11-07T23:16:48 | 296 | car | sb_lane_3 | 2.1s | medium | prolonged_overtaking_lane |
| 2026-11-07T23:16:58 | 376 | car | nb_lane_3 | 2.1s | medium | prolonged_overtaking_lane |
| 2026-11-07T23:17:16 | 433 | truck | sb_lane_3 | 0.4s | high | heavy_vehicle_in_overtaking_lane |
| 2026-11-07T23:17:21 | 472 | car | nb_lane_3 | 2.0s | medium | prolonged_overtaking_lane |
| 2026-11-07T23:18:56 | 863 | car | sb_lane_3 | 2.1s | medium | prolonged_overtaking_lane |

## Events by type

| Event type | Count |
| --- | --- |
| lane_enter | 86 |
| lane_exit | 86 |
| lane_change | 12 |
| lane_hogging_indication | 8 |

## Run metadata

| Key | Value |
| --- | --- |
| base_timestamp | 2026-11-07T23:15:38 |
| detector | yolo+bytetrack:yolo11n.pt |
| duration_s | 303.077 |
| frame_stride | 2 |
| frames_processed | 2728 |
| frames_read | 5456 |
| hogging_rules | ['heavy_vehicle_in_overtaking_lane', 'prolonged_overtaking_lane'] |
| lanes | [{'name': 'nb_lane_1', 'index': 1, 'role': 'normal', 'direction': 'northbound', 'polygon': [[0.0, 0.7658], [0.1576, 0.61896], [0.26709, 0.51766], [0.35983, 0.43587], [0.38075, 0.41822], [0.41004, 0.41822], [0.3947, 0.43773], [0.32845, 0.5158], [0.2629, 0.59387], [0.15411, 0.72491], [0.0, 0.92565]]}, {'name': 'nb_lane_2', 'index': 2, 'role': 'normal', 'direction': 'northbound', 'polygon': [[0.0, 0.92565], [0.0, 1.0], [0.13738, 1.0], [0.27406, 0.73141], [0.35704, 0.57063], [0.40028, 0.49349], [0.42957, 0.44145], [0.44212, 0.41822], [0.41004, 0.41822], [0.3947, 0.43773], [0.32845, 0.5158], [0.2629, 0.59387], [0.15411, 0.72491]]}, {'name': 'nb_lane_3', 'index': 3, 'role': 'overtaking', 'direction': 'northbound', 'polygon': [[0.13738, 1.0], [0.27406, 0.73141], [0.35704, 0.57063], [0.40028, 0.49349], [0.42957, 0.44145], [0.44212, 0.41822], [0.47768, 0.41822], [0.4749, 0.43494], [0.46304, 0.4842], [0.4477, 0.56041], [0.42469, 0.68866], [0.37308, 1.0]]}, {'name': 'sb_lane_1', 'index': 1, 'role': 'normal', 'direction': 'southbound', 'polygon': [[1.0, 0.68587], [0.77336, 0.51952], [0.72385, 0.48513], [0.6855, 0.45911], [0.62762, 0.41729], [0.62692, 0.41822], [0.58577, 0.41822], [0.60948, 0.44145], [0.69944, 0.52695], [0.78452, 0.61059], [0.88633, 0.71561], [1.0, 0.829]]}, {'name': 'sb_lane_2', 'index': 2, 'role': 'normal', 'direction': 'southbound', 'polygon': [[1.0, 0.829], [1.0, 1.0], [0.92678, 1.0], [0.77127, 0.75743], [0.68061, 0.6171], [0.61367, 0.51394], [0.55858, 0.43401], [0.54812, 0.41822], [0.58577, 0.41822], [0.60948, 0.44145], [0.69944, 0.52695], [0.78452, 0.61059], [0.88633, 0.71561]]}, {'name': 'sb_lane_3', 'index': 3, 'role': 'overtaking', 'direction': 'southbound', 'polygon': [[0.92678, 1.0], [0.77127, 0.75743], [0.68061, 0.6171], [0.61367, 0.51394], [0.55858, 0.43401], [0.54812, 0.41822], [0.50837, 0.41822], [0.51325, 0.43866], [0.52999, 0.50558], [0.55997, 0.63197], [0.59414, 0.77045], [0.64644, 1.0]]}] |
| output_fps | 9.001 |
| processing_fps | 8.1 |
| processing_seconds | 336.736 |
| run_id | km58_final |
| source_fps | 18.002 |
| source_video | data\raw\km58_400B_tj_priok_20261107_161545.mp4 |
| start_frame | 0 |

> Lane-hogging is a rule-based **indication** produced from class, lane, and
> dwell time. It is not a legal determination. Review the configuration against
> the actual road rules before using the output for enforcement.
