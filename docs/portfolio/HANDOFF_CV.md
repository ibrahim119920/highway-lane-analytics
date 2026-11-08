# Portfolio Handoff — CV / Résumé Writer

**Audience:** the agent writing the short portfolio entry and résumé bullets.
**Scope:** two completed projects that form one system.
**Language:** final copy is **English**. This brief mixes instructions and ready-to-paste copy.

> Read `HANDOFF_WEB.md` only if you need more depth. This file is deliberately short.

---

## 1. What the project is (one sentence)

A computer-vision pipeline that turns highway CCTV footage into lane-occupancy and
rule-based lane-hogging events, plus a data platform that streams those events through
Kafka and serves them in an analytics dashboard.

Two repositories:

| Part | What it does | Repo |
| --- | --- | --- |
| Vision | Detect, track, map to six lanes, emit events, annotate video, cut evidence | `ibrahim119920/highway-lane-analytics` |
| Data platform | Receive over HTTP, Kafka, DuckDB/Parquet, Streamlit dashboard | local project, not yet public |

If only one link is allowed, use the vision repo and describe the data platform as the
downstream half of the same system.

---

## 2. Copy blocks (pick one)

### 2.1 Twelve words

> Computer vision system detecting lane occupancy and rule-based hogging on highway CCTV.

### 2.2 Forty words

> Built an end-to-end computer vision pipeline that tracks vehicles with YOLO11n and
> ByteTrack, maps them to six calibrated highway lanes, and emits rule-based lane-hogging
> indications. A webhook replay then feeds Kafka, DuckDB, and a Streamlit dashboard for
> lane occupancy analytics.

### 2.3 Ninety words

> Designed a two-part traffic analytics system for a real night-time toll-road camera. The
> vision service detects and tracks cars, buses, and trucks with YOLO11n and ByteTrack,
> maps each vehicle to one of six calibrated lane polygons, and converts positions into
> lane entry, exit, change, and rule-based hogging events. A separate data platform receives those events over HTTP, streams them through Kafka, models them in
> DuckDB and Parquet, and serves a four-page Streamlit dashboard. On a five-minute clip it
> processed 158 vehicles and produced 111 events at 8.65 fps on CPU.

### 2.4 One hundred fifty words

> Highway Lane Occupancy & Hogging Detection is an end-to-end portfolio project built
> around a real night-time CCTV feed from Tj. Priok KM 58+400 in Jakarta. The
> computer-vision stage uses YOLO11n for detection and ByteTrack for persistent identities,
> then assigns every vehicle to one of six normalized lane polygons: three northbound and
> three southbound, with the median-side lane marked as overtaking. An adaptive anchor and
> a direction vote keep wide, near-field vehicles in the correct lane, while frame
> hysteresis prevents flicker at painted boundaries. From that state machine the same system
> emits lane entry, exit, change, and rule-based hogging events, plus per-track summaries,
> CSV/JSON/Markdown reports, an annotated video, and an eight-second snapshot-and-clip
> evidence bundle per event. The companion data platform receives those events over a
> token-authenticated webhook, streams them through Kafka, models them as bronze, silver,
> and gold layers in DuckDB and Parquet, and exposes them in a four-page Streamlit
> dashboard.

---

## 3. Résumé bullets

Use three to five. The first two are the strongest.

- Built a computer-vision pipeline (YOLO11n + ByteTrack) that assigns vehicles to six
  calibrated highway lanes and emits lane-entry, exit, change, and rule-based hogging
  events, backed by **90 automated tests**.
- Processed a five-minute 1024×768 night clip at **8.65 fps on CPU**, producing
  **158 vehicles, 111 events, 2 lane changes, and 5 medium hogging indications**.
- Added per-event evidence generation: a JPEG snapshot plus an **8-second annotated MP4
  clip**, with ffmpeg encoding and an OpenCV fallback, de-duplicating events that share a
  frame.
- Built the downstream data platform: **FastAPI** webhook receiver, **Kafka** topics keyed
  by camera, **DuckDB/Parquet** bronze-silver-gold layers, and a four-page
  **Streamlit/Plotly** dashboard; **19 tests**.
- Packaged a Google Colab workflow where `activate(source="drive_zip" | "live")` handles
  either a zipped batch of clips or a live HLS recording, then replays results to the
  webhook.

---

## 4. Numbers you may quote

| Metric | Value | Context |
| --- | --- | --- |
| Vehicles | 158 | 5-minute night clip, car 126 / truck 29 / bus 3 |
| Events | 111 | 52 enter, 52 exit, 2 lane changes, 5 hogging |
| Lane changes | 2 | one northbound, one southbound |
| Hogging indications | 5 | all `medium`, all `nb_lane_3`, rule `prolonged_overtaking_lane` |
| Peak event minute | 32 events | 23:27–23:28 local time |
| Throughput | 8.65 fps | CPU, 1024×768, frame stride 2, `imgsz 960` |
| Clip length | 299.7 s | 5,394 frames read, 2,697 processed |
| Second run | 18 vehicles / 20 events / 1 lane change / 1 hogging | 55-second daytime clip |
| Test suite | 109 tests | 90 vision + 19 data platform |
| Evidence bundle | 18 snapshots + 18 clips, 38.8 MB | 55-second run, 20 events, shared frames de-duplicated |

Do **not** round these up or present them as a benchmark. They come from one camera and
two short clips.

---

## 5. Skills to tag

Computer vision · object detection · multi-object tracking · YOLO11 · ByteTrack ·
OpenCV · Python · polygon lane calibration · rule-based event detection · JSONL event
contracts · FastAPI · Apache Kafka · DuckDB · Parquet · medallion architecture ·
Streamlit · Plotly · pandas · pytest · Google Colab · FFmpeg.

Primary tags if the site limits you to five: **Computer Vision, Python, YOLO/ByteTrack,
Kafka, DuckDB**.

---

## 6. Links and metadata

- Repository: `https://github.com/ibrahim119920/highway-lane-analytics`
- Demo video: **pending** — will be `annotated_demo.mp4` in `docs/portfolio/assets/`
- Dashboard screenshots: **pending**
- Live demo: none. Do not imply a hosted service.

Suggested project title: **Highway Lane Occupancy & Hogging Detection**
Suggested subtitle: *Computer vision + streaming data platform for toll-road CCTV*

---

## 7. Claims to avoid

These are the easy ways to overstate the project. Keep the honest wording.

| Do not say | Say instead |
| --- | --- |
| Real-time detection | Near-real-time **replay** of precomputed batch results |
| Detects traffic violations | Emits **rule-based hogging indications** |
| Measures vehicle speed | No speed estimation was built |
| Identifies vehicles / plates | No ANPR or re-identification across cameras |
| Production-ready / deployed | Local prototype, single camera, Docker for Kafka only |
| Accurate vehicle counts | ByteTrack IDs can switch; counts are a **lower bound** on volume |
| 99% accuracy | No labelled accuracy benchmark exists |

Also avoid naming the agency or implying the camera feed is public. Describe it as a
"real night-time toll-road camera" and, if useful, "Tj. Priok KM 58+400, Jakarta".

---

## 8. Tone

Concrete, engineering-first, no marketing adjectives. Prefer numbers and mechanisms over
"powerful" or "innovative". The interesting parts are the lane geometry, the adaptive
anchor, and the fact that the vision and data halves meet at a documented event contract.

---

## 9. Deliverable checklist

- [ ] One copy block chosen (12, 40, 90, or 150 words)
- [ ] 3–5 résumé bullets
- [ ] Skills/tags list
- [ ] Repo link
- [ ] No claim from section 7
- [ ] Placeholder note where the demo video and dashboard screenshots will go
