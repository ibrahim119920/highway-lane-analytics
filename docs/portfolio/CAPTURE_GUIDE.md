# Portfolio Capture Guide — Screenshot vs Screen Recording

This guide decides **when to capture a still** and **when to capture motion** for the
vision project and the downstream data pipeline + dashboard. It also lists the exact
scenes, specs, and storyboards so the portfolio assets stay consistent.

## 1. Decision rule

| Use a screenshot when the claim is... | Use a screen recording when the claim is... |
| --- | --- |
| A **state**: one frame, one dashboard tab, one table, one summary | A **process**: motion, sequence, interaction, or change over time |
| Readable at a glance and verifiable in one glance | Only believable when the viewer sees what happens next |
| A comparison: before/after, configured vs rendered, expected vs actual | A pipeline: replay → receiver → Kafka → dashboard |
| A terminal **final result** | Terminal **progress**, streaming logs, or live updates |
| A chart or KPI panel the viewer must read carefully | Chart hover, filter change, refresh, or new run appearing |
| A schema, file tree, code snippet, or architecture diagram | A vehicle moving through lanes, a lane change, or a hogging badge appearing |

Rule of thumb: **if the value is in the pixels of one moment, screenshot it; if the value
is in what happens next, record it.** When both are true, publish the screenshot in the
README/case study and the recording as the demo.

## 2. Asset specifications

### Screenshots

- **Format:** PNG (lossless). JPEG only for photos/video frames already compressed.
- **Size:** 1920×1080 preferred; dashboard at least 1600 px wide.
- **Crop:** remove browser chrome unless the URL/state matters.
- **Annotations:** 2–3 callouts maximum (arrow, box, label). Keep the original readable.
- **Theme:** one consistent dark/light theme across the set.
- **Secrets:** hide tokens, ngrok URLs with tokens, personal paths, and email addresses.
- **Naming:** `vision_*.png`, `dashboard_*.png`, `pipeline_*.png`.

### Screen recordings / clips

- **Format:** MP4, H.264, `+faststart`, no audio unless narrated.
- **Resolution/FPS:** 1080p, 30 fps (60 fps only for fast cursor motion).
- **Length:** 15–45 s per clip; 60 s maximum for a portfolio demo.
- **Size:** under 15 MB for web; use a poster image and click-to-play.
- **Captions:** burn in 3–6 short captions or provide an SRT; assume muted autoplay.
- **Cursor:** highlight clicks; zoom the browser/terminal to 125–150%.
- **No dead time:** cut model loading, installs, downloads, and idle waits.
- **Naming:** `vision_<scene>.mp4`, `pipeline_<scene>.mp4`, `dashboard_<scene>.mp4`.

### Export instead of screen-record when possible

- **Annotated traffic footage:** trim the generated MP4 with ffmpeg; do not record a
  video player. This keeps quality and removes player chrome.
- **Dashboard:** record the browser interaction (or capture with Playwright) because the
  value is the filter/refresh/run appearing.
- **Terminal:** screenshot the final summary; record only when the streaming progress or
  log sequence is the point.
- **Kaggle:** screenshot the final artifact list; record only the notebook run if the
  workflow itself is part of the story.

## 3. Vision project — shot list

### Screenshots

| Asset | What it proves | How to produce |
| --- | --- | --- |
| `vision_annotated_frame.png` | Boxes, IDs, lane labels in the sky, dwell, counters | Pause the annotated MP4 on a clear multi-vehicle frame; crop to 16:9 |
| `vision_lane_mapping.png` | Six calibrated lane polygons | `lane-analytics preview-lanes --frame 2160` → `output/lane_preview.jpg` |
| `vision_calibration_grid.png` | Repeatable calibration method | `lane-analytics calibrate --frame 2160` → `output/calibration/grid.jpg` |
| `vision_before_after.png` | Lane labels no longer cover vehicles | Side-by-side of the old label placement and the current sky labels |
| `vision_events_jsonl.png` | Typed event contract | Open `output/events/<run_id>.jsonl` in an editor; show 3–5 lines |
| `vision_tracks_jsonl.png` | Per-track stints and dwell | Open `output/tracks/<run_id>.jsonl`; show one track with `lane_stints` |
| `vision_summary_md.png` | Aggregated report | Open `output/reports/<run_id>/summary.md` |
| `vision_evidence_snapshot.png` | Event evidence | Open one `output/evidence/<run_id>/snapshots/*.jpg` with a hogging badge |
| `vision_terminal_summary.png` | Run result and throughput | Screenshot the final `Lane occupancy summary` block |
| `vision_tests.png` | Test coverage | Screenshot `pytest -q` final line (keep the count) |
| `vision_id_handover.png` | Parallel ID continuity (optional) | Open `tracks/<run_id>_id_handovers.jsonl` and show one `handover` line |

### Screen recordings / clips

| Asset | What it proves | Scene |
| --- | --- | --- |
| `vision_lane_change.mp4` | Lane entry/exit/change in motion | 10–15 s around a lane change; keep the box, ID, lane label, and dwell visible |
| `vision_hogging.mp4` | Hogging indication appears and persists | 8–12 s around a hogging event; red box and `! HOGGING` badge visible |
| `vision_evidence_clip.mp4` | Event evidence in context | Use one exported 8 s evidence clip; do not screen-record the player |
| `vision_live_kaggle.mp4` (optional) | Kaggle live-stream workflow | 20–30 s: settings → live cell → output artifacts |
| `vision_parallel_handover.mp4` (optional) | Parallel segments keep canonical IDs | 10–15 s: two segment logs / handover table with the same canonical ID |

**Vision demo storyboard (30 s):**

1. **0–3 s** — title card: project name + one-line problem.
2. **3–18 s** — annotated footage: one car enters lane 2, the label shows `nb_lane_2`
   and dwell; cut to a lane change or hogging badge.
3. **18–24 s** — split screen: annotated frame on the left, `events.jsonl` or terminal
   summary on the right.
4. **24–30 s** — end card with measured numbers: vehicles, events, hogging, CPU fps.

## 4. Pipeline + dashboard — shot list

### Screenshots

| Asset | What it proves | How to produce |
| --- | --- | --- |
| `pipeline_architecture.png` | End-to-end contract | Render the Mermaid/architecture diagram from the pipeline README |
| `pipeline_topics.png` | Kafka topics exist | `lane-pipeline topics` output |
| `pipeline_receiver_health.png` | Receiver auth + health | `curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:5000/health` |
| `pipeline_receiver_stats.png` | Accepted/duplicate counts | `/stats` response after a replay |
| `pipeline_consumer_logs.png` | Consumer is processing | Screenshot a short, readable consumer log window |
| `pipeline_verify_run.png` | Numbers reconcile | `python scripts/verify_run.py ...` with `31/31 PASS` |
| `pipeline_storage_tree.png` | Bronze/silver/gold layout | File tree of `data/bronze`, `data/silver`, `data/gold` |
| `dashboard_overview.png` | KPI cards + time series | Overview tab with a real run selected |
| `dashboard_lane_analysis.png` | Heatmap, box plot, lane usage | Lane analysis tab |
| `dashboard_hogging.png` | Hogging events + severity/rule breakdown | Hogging explorer tab |
| `dashboard_quality.png` | Run registry + data-quality checks | Run & data quality tab |
| `dashboard_filters.png` | Global filter panel | Crop the filter row with selected values visible |
| `pipeline_systemd_status.png` (optional) | Deployed services | `systemctl status lane-pipeline.target lane-receiver lane-consumer lane-dashboard` |
| `pipeline_docker_stats.png` (optional) | Resource footprint | `docker stats --no-stream lane-kafka` |

### Screen recordings / clips

| Asset | What it proves | Scene |
| --- | --- | --- |
| `pipeline_replay_to_dashboard.mp4` | End-to-end flow | 30–45 s: `lane-pipeline replay` → receiver logs → consumer logs → dashboard refresh shows the new run |
| `dashboard_filter_interaction.mp4` | Dashboard is usable | 15–20 s: change run/camera/direction/lane/class, charts update, hover a point |
| `dashboard_live_lag.mp4` (optional) | Near-real-time behavior | 10–15 s: replay a new batch, lag cards and time series update |
| `pipeline_kafka_lag.mp4` (optional) | Kafka consumer group works | 10 s: consumer group lag before/after replay |
| `pipeline_systemd_restart.mp4` (optional) | Operability | 10 s: restart a service, health check returns |

**Pipeline demo storyboard (45 s):**

1. **0–3 s** — title + mini architecture: vision → webhook → Kafka → DuckDB/Parquet →
   dashboard.
2. **3–12 s** — terminal: `lane-pipeline replay ... --speed 20` sending micro-batches.
3. **12–20 s** — receiver and consumer logs; show accepted batches and dedupe count.
4. **20–35 s** — dashboard Overview refreshes with the new run; switch to Hogging
   explorer; change one global filter so the charts visibly update.
5. **35–42 s** — Run & data quality tab; show `verify_run.py` `31/31 PASS`.
6. **42–45 s** — end card with the contract: **near-real-time replay**, not live
   inference; **rule-based hogging indication**, not a legal determination.

## 5. What not to capture

- Installing dependencies, downloading models, or waiting for Kafka to start.
- A full 5-minute annotated video; trim to the 10–20 s that proves the claim.
- Scrolling through code or logs without a highlighted line.
- Empty dashboards, zero-state charts, or errors that are not the point.
- Tokens, `.env` values, ngrok URLs with secrets, personal paths, email addresses.
- Synthetic demo data presented as real footage; label it **synthetic** if used.
- More than 2–3 callouts per screenshot; let the original breathe.

## 6. Where each asset goes

| Placement | Use |
| --- | --- |
| README hero | `vision_annotated_frame.png` + 20 s `vision_lane_change.mp4` or GIF |
| Portfolio case study | 1 hero video, 4–6 screenshots, 2 short videos (vision + pipeline) |
| CV / résumé | One screenshot + link to a 60 s demo |
| LinkedIn | Native 30–60 s MP4 with burned-in captions |
| GitHub docs | PNGs under `docs/portfolio/assets/`; videos under `docs/sample_output/` or an external link |
| Dashboard page | `dashboard_overview.png`, `dashboard_hogging.png`, `pipeline_replay_to_dashboard.mp4` |

## 7. Capture QA checklist

- [ ] The numbers match `summary.json` / the README (vehicles, events, hogging, fps).
- [ ] The data is labelled real or synthetic.
- [ ] No secrets, tokens, or personal paths are visible.
- [ ] Screenshots are PNG and at least 1600 px wide for the dashboard.
- [ ] Videos are H.264 MP4, 15–45 s, under 15 MB, with captions.
- [ ] The cursor/annotations point at the thing being claimed.
- [ ] The first 2 seconds already show the value; no logo intro.
- [ ] Alt text and a one-line caption exist for every asset.
- [ ] File names follow the manifest in `docs/portfolio/README.md`.
