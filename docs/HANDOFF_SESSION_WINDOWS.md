# Handoff Prompt — Session and Window Support

Copy the prompt below to the agent working on
`highway-lane-analytics-data-pipeline` (FastAPI receiver, Kafka, DuckDB/Parquet,
Streamlit dashboard). It is self-contained and includes the contract from the vision
repo.

---

## Prompt for the pipeline & dashboard agent

You are implementing **session/window support** end-to-end in
`highway-lane-analytics-data-pipeline`. The vision repo now emits optional session
metadata. Do not change the vision repo; consume its contract.

### Context

The vision pipeline can process a live stream or a long video in 30-second windows. A
single user action (one notebook cell start, or one long-video batch) is a **session**.
Each 30-second chunk is a **run** (window). This preserves the existing idempotency key
while allowing the dashboard to group windows.

```text
session_id = live_20261108_082400          # one click / one recording session
run_id     = live_20261108_082400_w0000    # window 0, owned by session
run_id     = live_20261108_082400_w0001    # window 1
window_index = 0, 1, ...
window_count = total planned windows
```

Rules:

- `run_id` remains **unique per window** and remains the supersede key.
- `session_id` is shared by all windows of one click/session.
- Track IDs reset per window. Do **not** sum distinct `track_id` across windows as
  unique vehicles; label session totals as window-scoped or sum per-window counts.
- Event-time remains the real event time. `session_id` is grouping metadata, not a
  timestamp.

Events and track summaries now include these optional fields:

```json
{
  "session_id": "live_20261108_082400",
  "window_index": 0,
  "window_count": 3
}
```

- Legacy 1.0 events omit them or send empty/`null`.
- New session-aware events use `schema_version: "1.1"`.
- Run metadata (`summary.json` → `run`) and `run_config.json` also include
  `session_id`, `window_index`, `window_count`.

### Required changes

1. **Models / validation**
   - Add optional `session_id: str`, `window_index: int | null`,
     `window_count: int | null` to event, track, and run models.
   - Accept both schema 1.0 and 1.1. Do not reject 1.0.
   - Validate `window_index >= 0`, `window_count >= 1`, and
     `window_index < window_count` when both are present.

2. **Receiver and dedupe**
   - Preserve the new fields in the bronze JSONL.
   - Keep `event_id` as the event dedupe key.
   - Do not use `run_id` to dedupe events; a run may be replayed.
   - Return the accepted session/window fields in `/stats` if convenient.

3. **DuckDB / Parquet**
   - Add `session_id`, `window_index`, `window_count` to `fact_event`,
     `fact_track`, and `fact_run` (or add a `dim_session` and join).
   - Add a migration path for an existing `pipeline.duckdb`
     (`ALTER TABLE ... ADD COLUMN` or recreate the affected tables/views).
   - Keep `run_id` unique. Add indexes on `(camera_id, session_id, window_index)`.
   - Silver Parquet: include the fields in the exported facts.
   - Gold: add session-level aggregates, for example
     `agg_session_summary` (camera, session, windows_seen, windows_expected,
     first_event_ts, last_event_ts, events, vehicles_window_scoped, hogging).
   - `verify_run.py`: compare the new fields where present; do not fail legacy runs.

4. **Ingest and replay**
   - `lane-pipeline ingest` and the replay sender must preserve the fields.
   - Run registry: one row per `run_id`, with `session_id`, `window_index`,
     `window_count`, `session_started_at`, and a session-complete flag computed from
     the expected window count.
   - Supersede remains per `run_id`. Re-ingesting one window must not delete other
     windows in the same session.

5. **Dashboard**
   - Add a **Session** filter and a **Window** column.
   - Default view: latest active **session** per camera, not latest run only.
     Alternatively, keep the latest-run default but expose a clearly labelled
     "Session" selector and a window timeline.
   - Add a session timeline showing `window_index` in order, events per window,
     missing windows, and session completion.
   - Overview KPI:
     - per-window vehicle counts and a session total labelled
       "tracks (window-scoped)",
     - lane-entry count per session,
     - lane changes per session,
     - hogging per session and per 1,000 lane-entry vehicles,
     - last event time and ingest lag per session/window.
   - Hogging explorer and Run & data quality: filter by session and show the window.
   - Legacy data: group by `run_id` as a one-window session
     (`session_id = run_id`, `window_index = 0`, `window_count = 1`) so old rows still
     render.

6. **Compatibility**
   - Existing tests, payloads, and dashboards must keep working for 1.0 data.
   - Missing `window_count` must not crash: treat it as unknown, not zero.
   - Do not break receiver auth, dedupe, Kafka topics, or the file-sink fallback.

7. **Tests**
   - Receiver accepts 1.1 fields and legacy 1.0.
   - Dedupe by `event_id` still works with session fields present.
   - DuckDB migration on an existing DB preserves rows.
   - Replay/ingest preserves session/window fields.
   - A 3-window session produces 3 runs under 1 session, with the correct
     `window_index`/`window_count`.
   - A missing window is visible in the dashboard query or session aggregate.
   - Legacy run renders without `session_id`.
   - `python -m pytest -q` and `python -m compileall -q src dashboard scripts tests`
     pass.

8. **Docs**
   - Copy/update `schemas/lane_event.schema.json` from the vision repo (version 1.1
     fields documented).
   - Update the handoff/data-contract docs with the session/window semantics.
   - State explicitly: `run_id` = one 30-second window; `session_id` = one click or one
     long-video batch; track IDs reset per window; vehicle totals are window-scoped.

### Acceptance criteria

- Replay a 3-window session: receiver accepts all three, DuckDB stores 3 runs under
  1 session, and the dashboard shows one session with 3 ordered windows.
- Per-window KPIs and session totals are visible and labelled correctly.
- A legacy 1.0 run still renders and verifies.
- Superseding one window does not delete the other windows in the session.
- All tests pass.

### Useful commands

```powershell
lane-pipeline init-db
lane-pipeline topics
lane-pipeline serve
lane-pipeline consume
lane-pipeline replay --events ... --tracks ... --summary ... --run-config ... --url ... --speed 20
lane-pipeline dashboard
python scripts/verify_run.py --db data/pipeline.duckdb ...
python -m pytest -q
```

---

## Contract examples

Event:

```json
{
  "camera_id": "tj_priok_km58_400b",
  "timestamp": "2026-11-08T08:24:48",
  "track_id": 12,
  "object_class": "car",
  "confidence": 0.91,
  "event_type": "lane_enter",
  "lane_name": "nb_lane_2",
  "schema_version": "1.1",
  "session_id": "live_20261108_082400",
  "window_index": 0,
  "window_count": 3
}
```

Track summary:

```json
{
  "camera_id": "tj_priok_km58_400b",
  "track_id": 12,
  "object_class": "car",
  "first_frame": 40,
  "last_frame": 300,
  "session_id": "live_20261108_082400",
  "window_index": 0,
  "window_count": 3,
  "lane_stints": []
}
```

Run metadata (`summary.json` → `run`):

```json
{
  "run_id": "live_20261108_082400_w0000",
  "session_id": "live_20261108_082400",
  "window_index": 0,
  "window_count": 3
}
```
