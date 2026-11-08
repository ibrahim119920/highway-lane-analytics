# Portfolio Package — Highway Lane Occupancy & Hogging Detection

Two handoff documents and the media assets for publishing the project as a portfolio case
study. Both projects are treated as complete; photos and videos that are still missing are
listed as **PENDING** with their target filenames and specs.

## Documents

| File | For | What it contains |
| --- | --- | --- |
| [`HANDOFF_CV.md`](./HANDOFF_CV.md) | CV / résumé writer | 12/40/90/150-word copy, résumé bullets, quotable metrics, skills, links, claims to avoid |
| [`HANDOFF_WEB.md`](./HANDOFF_WEB.md) | Portfolio web editor | Page metadata, hero and section copy, architecture diagram, deep dives, dashboard description, results, challenges, gallery, SEO, page order |
| [`CAPTURE_GUIDE.md`](./CAPTURE_GUIDE.md) | Whoever records the demo | Screenshot vs screen-record rules, exact scene list, specs, storyboards, QA checklist |
| `README.md` | Both | Index and asset manifest |

## Project in one paragraph

A computer-vision pipeline turns highway CCTV into typed lane events: which lane a vehicle
entered, when it left, whether it changed lanes, and whether it matched a rule-based
lane-hogging condition. The vision stage uses YOLO11n and ByteTrack over six calibrated
lane polygons, with an adaptive anchor for wide near-field boxes and frame hysteresis at
lane boundaries. A separate data platform receives the events through a token-authenticated
webhook, streams them through Kafka, models them in DuckDB and Parquet, and serves a
four-page Streamlit dashboard. Every flagged event gets a JPEG snapshot and an eight-second
annotated clip.

## Headline numbers

| Metric | Value |
| --- | --- |
| Vehicles tracked, 5-minute night clip | 158 (126 car, 29 truck, 3 bus) |
| Events | 111 (52 enter, 52 exit, 2 lane changes, 5 hogging) |
| Throughput | 8.65 fps on CPU, 1024×768, stride 2 |
| Second run, 55-second day clip | 18 vehicles, 20 events, 1 lane change, 1 hogging |
| Tests | 109 (90 vision + 19 data platform) |
| Evidence, 55-second run | 18 snapshots, 18 clips, 38.8 MB |

## Repositories

| Part | Location |
| --- | --- |
| Vision pipeline | `https://github.com/ibrahim119920/highway-lane-analytics` |
| Data platform | local project `highway-lane-analytics-data-pipeline`, not yet public |

The vision repository only sends data. The receiver, Kafka, consumer, storage, and
dashboard live in the data platform project.

## Asset manifest

All images below are in `docs/portfolio/assets/`.

### Available

| File | Dimensions | Used in | Description |
| --- | --- | --- | --- |
| `lane_mapping.jpg` | 1434×1076 | Hero, lane geometry | Six lane polygons on the calibration reference frame |
| `lane_preview.jpg` | 1024×768 | Lane geometry | Same polygons on a real camera frame |
| `annotated_frame.jpg` | 1024×768 | Results | Annotated frame with boxes, labels, and counters |

### Pending

| File | Required spec | Used in | What it should show |
| --- | --- | --- | --- |
| `annotated_demo.mp4` | 20–30 s, 1280×720, H.264, no audio | Results | Boxes, IDs, lane labels, dwell time, hogging badge |
| `evidence_hogging.jpg` | 1024×768 JPEG | Evidence | Hogging snapshot with the red box and `! HOGGING medium` badge |
| `evidence_hogging.mp4` | 8 s, 1024×768, H.264 | Evidence | Same event with motion before and after |
| `dashboard_overview.png` | ≥1600 px wide | Dashboard | Overview KPIs and charts |
| `dashboard_hogging.png` | ≥1600 px wide | Dashboard | Hogging explorer table and breakdown |
| `dashboard_quality.png` | ≥1600 px wide | Dashboard | Run registry and data-quality checks |
| `pipeline_diagram.png` | optional, 2000×900 | Architecture | Rendered diagram if Mermaid cannot be used |

When a file arrives, keep the exact filename so the page build resolves it. If a clip is
too large, re-encode to H.264 with a faststart flag and keep it under 15 MB.

The full capture plan — when to use a screenshot vs a screen recording, optional
pipeline/dashboard clips, specs, and storyboards — is in
[`CAPTURE_GUIDE.md`](./CAPTURE_GUIDE.md). The required page-build filenames above remain
the source of truth.

## How the available images were produced

| Asset | Source |
| --- | --- |
| `lane_mapping.jpg` | `output/calibration/build_user_lanes.py`, rendered at 1434×1076 |
| `lane_preview.jpg` | `lane-analytics preview-lanes` on the daytime clip, frame 0 |
| `annotated_frame.jpg` | Frame from the annotated 55-second run |

## Claim rules

These apply to both documents:

- **Near-real-time replay**, not live inference.
- **Rule-based hogging indication**, not a legal violation.
- **Prototype**, not a production deployment.
- No speed estimation, ANPR, or cross-camera re-identification was built.
- Vehicle counts are a lower bound because tracker IDs can switch after occlusion.

## Publishing checklist

- [ ] Choose one copy block from `HANDOFF_CV.md` for the short entry
- [ ] Build the page in the order from `HANDOFF_WEB.md` section 14
- [ ] Drop in the pending media using the filenames above
- [ ] Keep the hogging disclaimer visible wherever hogging is discussed
- [ ] Use the repo link, and describe the data platform as the downstream half
- [ ] Check meta description and Open Graph image crop (1200×630)
