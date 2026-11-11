"""Replay precomputed lane events to the production pipeline webhook.

This module is the *sender* side of the handoff. It never receives data and it has
no knowledge of Kafka. It reads the batch artifacts produced by ``analyze`` and
posts them as micro-batches to the receiver, normally through a Cloudflare Tunnel
(``https://lane-api.syaifudin-fam.my.id``).

Contract (see ``docs/HANDOFF_DATA_PIPELINE_DASHBOARD.md`` section 4.5):

1. ``POST /webhook``         - events, in ``video_time_s`` order, <= 200 per batch.
2. ``POST /webhook/tracks``  - once, after every event batch.
3. ``POST /webhook/run``     - last, with ``complete: true`` to trigger aggregates.

The sender is deliberately strict: it refuses to send events whose ``timestamp``
carries a timezone offset, whose ``camera_id`` differs from the envelope, or whose
``event_type`` is not in the schema, because the receiver answers those with 422
and the run would silently lose data.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://lane-api.syaifudin-fam.my.id"
DEFAULT_TIMEOUT = 30.0
DEFAULT_BATCH_SIZE = 200
MAX_BATCH_SIZE = 2000
# Cloudflare returns 52x while the origin/tunnel is warming up; those are retries,
# not payload errors. 4xx validation errors are never retried.
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
ENVELOPE_SOURCE = "kaggle-replay"
DEFAULT_SOURCE = ENVELOPE_SOURCE
EVENT_TYPES = {
    "lane_enter",
    "lane_exit",
    "lane_change",
    "lane_hogging_indication",
}
# Fixed namespace so a missing event_id can be reconstructed identically on retry.
_EVENT_NAMESPACE = uuid.UUID("6f3a1f9c-6c1e-4d0f-9f2b-3f9f6e0a5b21")


def resolve_base_url(value: str | None = None) -> str:
    """Resolve the receiver base URL.

    Priority: explicit argument, ``PIPELINE_BASE_URL``, legacy ``WEBHOOK_URL``,
    then the production Cloudflare hostname.
    """
    candidate = (value or "").strip()
    if candidate:
        return candidate.rstrip("/")
    for variable in ("PIPELINE_BASE_URL", "WEBHOOK_URL"):
        candidate = (os.getenv(variable) or "").strip()
        if candidate:
            return candidate.rstrip("/")
    return DEFAULT_BASE_URL


def resolve_token(value: str | None = None) -> str | None:
    """Resolve the bearer token from the argument or the environment."""
    candidate = (value or "").strip()
    if candidate:
        return candidate
    for variable in ("PIPELINE_WEBHOOK_TOKEN", "WEBHOOK_TOKEN"):
        candidate = (os.getenv(variable) or "").strip()
        if candidate:
            return candidate
    return None


class ReplayError(RuntimeError):
    """Raised when a replay cannot be completed."""


@dataclass
class Batch:
    """A group of events sharing one event-time window."""

    index: int
    offset_s: float
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReplayResult:
    run_id: str
    camera_id: str = ""
    events_sent: int = 0
    event_batches: int = 0
    tracks_sent: int = 0
    track_batches: int = 0
    run_sent: bool = False
    retries: int = 0
    duplicates: int = 0
    rejected: int = 0
    accepted: int = 0
    media_events: int = 0
    elapsed_s: float = 0.0
    event_span_s: float = 0.0
    dry_run: bool = False
    started_at: str = ""
    finished_at: str = ""
    batches_dir: str | None = None
    base_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "camera_id": self.camera_id,
            "events_sent": self.events_sent,
            "event_batches": self.event_batches,
            "tracks_sent": self.tracks_sent,
            "track_batches": self.track_batches,
            "run_sent": self.run_sent,
            "retries": self.retries,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "accepted": self.accepted,
            "media_events": self.media_events,
            "elapsed_s": round(self.elapsed_s, 3),
            "event_span_s": round(self.event_span_s, 3),
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "batches_dir": self.batches_dir,
            "base_url": self.base_url,
        }


# --------------------------------------------------------------- input helpers
def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        raise ReplayError(f"JSONL file not found: {source}")
    records: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"{source}:{number} is not valid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ReplayError(f"{source}:{number} must be a JSON object")
            records.append(value)
    return records


def load_summary(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise ReplayError(f"Summary file not found: {source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplayError(f"{source} must contain a JSON object")
    return value


def event_offset(event: dict[str, Any]) -> float | None:
    """Best-effort event time in source seconds."""
    value = event.get("video_time_s")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return None


def _parse_naive_timestamp(value: Any, *, index: int) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ReplayError(f"event {index}: timestamp is required")
    if text.endswith(("Z", "z")):
        raise ReplayError(
            f"event {index}: timestamp must be naive local time (WIB), not UTC: {text!r}"
        )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReplayError(f"event {index}: timestamp is not ISO-8601: {text!r}") from exc
    if parsed.tzinfo is not None:
        raise ReplayError(
            f"event {index}: timestamp must not carry a timezone offset: {text!r}"
        )
    return parsed


def ensure_event_ids(
    events: Iterable[dict[str, Any]],
    *,
    run_id: str = "",
    camera_id: str = "",
) -> list[dict[str, Any]]:
    """Return copies with a stable ``event_id``.

    Events produced by ``analyze`` already carry a UUID4. When one is missing we
    derive a UUID5 from the event's identity instead of a random value, so a retry
    (or a re-read of the same artifact) produces the same dedupe key.
    """
    resolved: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        if not item.get("event_id"):
            identity = "|".join(
                str(part)
                for part in (
                    run_id,
                    camera_id or item.get("camera_id", ""),
                    item.get("track_id", ""),
                    item.get("event_type", ""),
                    item.get("frame_index", ""),
                    item.get("timestamp", ""),
                    item.get("lane_name", ""),
                    item.get("previous_lane", ""),
                    item.get("video_time_s", ""),
                )
            )
            item["event_id"] = uuid.uuid5(_EVENT_NAMESPACE, identity).hex
        resolved.append(item)
    return resolved


def validate_events(
    events: Sequence[dict[str, Any]],
    *,
    camera_id: str,
    run_id: str = "",
) -> list[dict[str, Any]]:
    """Normalise and validate events against the receiver contract.

    Missing ``camera_id`` is filled from the envelope; a conflicting one is an
    error because the receiver rejects the whole event with 422.
    """
    if not events:
        raise ReplayError("no events to send")
    if not camera_id:
        raise ReplayError("camera_id is required for the envelope")

    validated: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        item = dict(event)
        item_camera = str(item.get("camera_id") or "").strip()
        if not item_camera:
            item["camera_id"] = camera_id
        elif item_camera != camera_id:
            raise ReplayError(
                f"event {index}: camera_id {item_camera!r} differs from envelope "
                f"{camera_id!r}; the receiver would reject it"
            )

        event_type = str(item.get("event_type") or "").strip()
        if event_type not in EVENT_TYPES:
            raise ReplayError(
                f"event {index}: unknown event_type {event_type!r}; "
                f"expected one of {sorted(EVENT_TYPES)}"
            )

        _parse_naive_timestamp(item.get("timestamp"), index=index)

        window_index = item.get("window_index")
        window_count = item.get("window_count")
        if window_index is not None and window_count is not None:
            try:
                invalid = int(window_index) >= int(window_count)
            except (TypeError, ValueError):
                invalid = True
            if invalid:
                raise ReplayError(
                    f"event {index}: window_index {window_index!r} must be smaller "
                    f"than window_count {window_count!r}"
                )

        if run_id and item.get("run_id") and str(item["run_id"]) != run_id:
            raise ReplayError(
                f"event {index}: run_id {item['run_id']!r} differs from envelope {run_id!r}"
            )
        validated.append(item)
    return validated


def validate_tracks(
    tracks: Sequence[dict[str, Any]],
    *,
    camera_id: str,
    run_id: str = "",
) -> list[dict[str, Any]]:
    """Validate the track summaries posted to ``/webhook/tracks``."""
    validated: list[dict[str, Any]] = []
    for index, track in enumerate(tracks):
        item = dict(track)
        if item.get("track_id") is None:
            raise ReplayError(f"track {index}: track_id is required")
        item_camera = str(item.get("camera_id") or "").strip()
        if item_camera and item_camera != camera_id:
            raise ReplayError(
                f"track {index}: camera_id {item_camera!r} differs from envelope {camera_id!r}"
            )
        item_run = str(item.get("run_id") or "").strip()
        if run_id and item_run and item_run != run_id:
            raise ReplayError(
                f"track {index}: run_id {item_run!r} differs from envelope {run_id!r}"
            )
        validated.append(item)
    return validated


def _event_order(event: dict[str, Any], fallback: int) -> tuple[float, int]:
    offset = event_offset(event)
    if offset is None:
        frame = event.get("frame_index")
        if frame is not None:
            try:
                return float(frame), fallback
            except (TypeError, ValueError):
                pass
        timestamp = event.get("timestamp")
        if isinstance(timestamp, str):
            try:
                parsed = datetime.fromisoformat(timestamp)
                return parsed.timestamp(), fallback
            except ValueError:
                pass
        return float(fallback), fallback
    return offset, fallback


def group_windows(
    events: Sequence[dict[str, Any]],
    *,
    window_seconds: float = 5.0,
    max_batch: int = 200,
    start_at_first_event: bool = True,
) -> list[Batch]:
    """Split events into event-time windows for paced replay.

    Events keep their original order for identical timestamps (a lane change is
    emitted as ``lane_exit`` -> ``lane_change`` -> ``lane_enter``).
    """
    if window_seconds <= 0:
        raise ReplayError("window_seconds must be greater than zero")
    if max_batch < 1:
        raise ReplayError("max_batch must be at least 1")
    if not events:
        return []

    order = {id(event): index for index, event in enumerate(events)}
    ordered = sorted(events, key=lambda event: _event_order(event, order[id(event)]))
    base = event_offset(ordered[0]) or 0.0
    if start_at_first_event:
        # Keep the first event at offset 0 so replay does not idle before it appears.
        pass
    else:
        base = 0.0

    batches: list[Batch] = []
    current: Batch | None = None
    for event in ordered:
        offset = event_offset(event)
        if offset is None:
            offset = 0.0
        relative = max(0.0, offset - base)
        bucket = int(relative // window_seconds)
        if current is None or bucket != int(current.offset_s // window_seconds):
            current = Batch(index=len(batches), offset_s=relative)
            batches.append(current)
        current.events.append(event)
        if len(current.events) >= max_batch:
            current = None
    return batches


# --------------------------------------------------------------- HTTP helpers
def endpoint(base: str, path: str) -> str:
    """Join a base URL (or a full /webhook URL) with a contract path."""
    value = base.strip().rstrip("/")
    if value.endswith("/webhook"):
        value = value[: -len("/webhook")]
    if not value:
        raise ReplayError("webhook URL is empty")
    return f"{value}{path}"


def build_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "ngrok-skip-browser-warning": "true",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _response_text(response: requests.Response, limit: int = 300) -> str:
    try:
        text = response.text
    except Exception:  # pragma: no cover - defensive
        return ""
    return text[:limit].replace("\n", " ")


def post_json(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: float,
    retries: int,
    sleep: Callable[[float], None] = time.sleep,
    base_delay: float = 1.0,
) -> tuple[requests.Response, int]:
    """POST with retry/backoff. Returns the response and the retry count."""
    attempts = max(0, retries)
    last_error: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            response = session.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = min(base_delay * (2**attempt), 30.0) + random.uniform(0.0, 0.3)
            log.warning("POST %s failed (%s); retrying in %.1fs", url, exc, delay)
            sleep(delay)
            continue

        if response.status_code in (200, 201, 202):
            return response, attempt
        if response.status_code in RETRY_STATUS and attempt < attempts:
            delay = min(base_delay * (2**attempt), 30.0) + random.uniform(0.0, 0.3)
            log.warning(
                "POST %s returned %s (%s); retrying in %.1fs",
                url,
                response.status_code,
                _response_text(response, 120),
                delay,
            )
            sleep(delay)
            continue
        raise ReplayError(
            f"POST {url} failed with {response.status_code}: {_response_text(response)}"
        )

    raise ReplayError(f"POST {url} failed after {attempts + 1} attempts: {last_error}")


def post_media_patch(
    media_result: Any,
    *,
    url: str | None = None,
    token: str | None = None,
    run_id: str | None = None,
    camera_id: str | None = None,
    source: str = "kaggle-media",
    retries: int = 2,
    timeout: float = 20.0,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Attach already-uploaded clips to events that were replayed earlier.

    Events are sent as soon as a window is processed; this patch carries the
    media rows afterwards. It is safe to skip or retry because the pipeline
    upserts on ``media_id``.
    """
    ready = {"COMPLETED", "PARTIAL", "SKIPPED"}
    entries: list[dict[str, Any]] = []
    skipped = 0
    for entry in getattr(media_result, "entries", None) or []:
        status = str(getattr(entry, "status", "") or "")
        event_ids = [
            str(event_id)
            for event_id in (getattr(entry, "event_ids", None) or [])
            if event_id
        ]
        media_id = str(getattr(entry, "media_id", "") or "")
        video_path = str(getattr(entry, "video_path", "") or "")
        if status not in ready or not event_ids or not media_id or not video_path:
            skipped += 1
            continue
        entries.append(
            {
                "media_id": media_id,
                "event_ids": event_ids,
                "video_path": video_path,
                "thumbnail_path": getattr(entry, "thumbnail_path", None),
                "file_size": getattr(entry, "file_size", None),
                "duration_s": getattr(entry, "duration_s", None),
                "status": status,
            }
        )

    run_id = run_id or str(getattr(media_result, "run_id", "") or "")
    camera_id = camera_id or str(getattr(media_result, "camera_id", "") or "")
    if not entries:
        return {"sent": 0, "entries": 0, "skipped": skipped, "dry_run": dry_run}
    if dry_run:
        return {"sent": 0, "entries": len(entries), "skipped": skipped, "dry_run": True}

    base = resolve_base_url(url)
    if not token:
        raise ReplayError("Media patch requires a pipeline token")
    payload = {
        "run_id": run_id,
        "camera_id": camera_id,
        "source": source,
        "sent_at": _utc_now(),
        "media": entries,
    }
    client = session or requests.Session()
    target = endpoint(base, "/webhook/media")
    response, retry_count = post_json(
        client,
        target,
        payload,
        headers=build_headers(token),
        timeout=timeout,
        retries=retries,
    )
    return {
        "sent": len(entries),
        "entries": len(entries),
        "skipped": skipped,
        "retries": retry_count,
        "status": response.status_code,
        "url": target,
    }


def check_health(
    base: str,
    *,
    token: str | None = None,
    timeout: float = 5.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """GET ``/health`` and raise when the receiver or its Kafka sink is unhealthy."""
    client = session or requests.Session()
    url = endpoint(base, "/health")
    try:
        response = client.get(url, headers=build_headers(token), timeout=timeout)
    except requests.RequestException as exc:
        raise ReplayError(
            f"Pipeline is not reachable at {url}. Check the Cloudflare tunnel and network.\n{exc}"
        ) from exc
    if response.status_code != 200:
        raise ReplayError(
            f"Pipeline health check failed ({response.status_code}): "
            f"{_response_text(response)}"
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:200]}
    if payload.get("healthy") is False:
        raise ReplayError(f"Pipeline reports unhealthy: {payload}")
    sink = payload.get("sink") or {}
    sink_status = sink.get("status") if isinstance(sink, dict) else None
    if sink_status is not None and sink_status != "ok":
        raise ReplayError(f"Pipeline sink is not ready: {sink}")
    log.info("Pipeline healthy: %s", payload)
    return payload


def wait_healthy(
    base: str,
    *,
    token: str | None = None,
    attempts: int = 6,
    delay: float = 10.0,
    timeout: float = 10.0,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Retry ``/health`` until Kafka is ready, then return the payload."""
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return check_health(
                base, token=token, timeout=timeout, session=session
            )
        except ReplayError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            log.warning(
                "Pipeline not ready (%d/%d): %s; retrying in %.0fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            sleep(delay)
    raise ReplayError(
        f"Pipeline/Kafka not ready after {attempts} attempt(s): {last_error}"
    )


def fetch_stats(
    base: str,
    *,
    token: str | None = None,
    timeout: float = 10.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """GET ``/stats`` to verify accepted/duplicate/rejected counters."""
    client = session or requests.Session()
    url = endpoint(base, "/stats")
    try:
        response = client.get(url, headers=build_headers(token), timeout=timeout)
    except requests.RequestException as exc:
        raise ReplayError(f"Could not read stats at {url}: {exc}") from exc
    if response.status_code != 200:
        raise ReplayError(
            f"GET {url} failed with {response.status_code}: {_response_text(response)}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ReplayError(f"GET {url} did not return JSON") from exc


# --------------------------------------------------------------- replay
def _envelope(
    run_id: str,
    camera_id: str,
    key: str,
    items: list[dict[str, Any]],
    source: str = ENVELOPE_SOURCE,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "camera_id": camera_id,
        "source": source,
        "sent_at": _utc_now(),
        key: items,
    }


def _post_batch(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout: float,
    retries: int,
    sleep: Callable[[float], None],
) -> tuple[dict[str, Any], int]:
    """POST one batch and refuse a 202 that silently dropped events."""
    response, used = post_json(
        session,
        url,
        payload,
        headers=headers,
        timeout=timeout,
        retries=retries,
        sleep=sleep,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    rejected = int(body.get("rejected") or 0)
    if rejected:
        raise ReplayError(
            f"POST {url} accepted the request but rejected {rejected} item(s); "
            f"fix the payload before resending: {body}"
        )
    return body, used


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def replay_run(
    events_path: str | Path,
    *,
    tracks_path: str | Path | None = None,
    summary_path: str | Path | None = None,
    config_path: str | Path | None = None,
    summary_data: dict[str, Any] | None = None,
    config_data: dict[str, Any] | None = None,
    url: str | None = None,
    token: str | None = None,
    run_id: str | None = None,
    source: str | None = None,
    speed: float = 1.0,
    window_seconds: float = 5.0,
    max_batch: int = DEFAULT_BATCH_SIZE,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 5,
    start_at_first_event: bool = True,
    skip_health_check: bool = False,
    health_attempts: int = 6,
    health_delay: float = 10.0,
    complete: bool = True,
    validate: bool = True,
    media_manifest: dict[str, dict[str, Any]] | None = None,
    dry_run: bool = False,
    dry_run_dir: str | Path | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ReplayResult:
    """Replay one run's events (and tracks/run metadata) to the pipeline.

    ``summary_data``/``config_data`` accept the dicts already in memory, so a
    caller that skipped report writing (for example the per-window Kaggle loop)
    can still send ``/webhook/run`` and mark the run complete.
    """
    if speed <= 0:
        raise ReplayError("speed must be greater than zero")
    if max_batch < 1 or max_batch > MAX_BATCH_SIZE:
        raise ReplayError(f"max_batch must be between 1 and {MAX_BATCH_SIZE}")

    events_file = Path(events_path)
    events = load_jsonl(events_file)
    if not events and not dry_run:
        raise ReplayError(f"{events_file} contains no events")

    effective_run_id = run_id or events_file.stem
    envelope_source = (
        (source or os.getenv("PIPELINE_SOURCE") or ENVELOPE_SOURCE).strip()
        or ENVELOPE_SOURCE
    )

    summary = summary_data
    if summary is None and summary_path:
        summary = load_summary(summary_path)
    config = config_data
    if config is None and config_path:
        config = load_summary(config_path)

    camera_id = ""
    for event in events:
        if event.get("camera_id"):
            camera_id = str(event["camera_id"]).strip()
            break
    if not camera_id and summary:
        run_meta = summary.get("run") or {}
        camera_id = str(run_meta.get("camera_id") or summary.get("camera_id") or "").strip()
    if not camera_id:
        raise ReplayError("events do not contain a camera_id")

    if summary is not None:
        run_meta = summary.get("run") or {}
        summary_run_id = str(run_meta.get("run_id") or "").strip()
        if summary_run_id and summary_run_id != effective_run_id:
            raise ReplayError(
                f"summary run_id {summary_run_id!r} differs from envelope "
                f"{effective_run_id!r}; pass run_id= to align them"
            )
        summary_camera = str(
            run_meta.get("camera_id") or summary.get("camera_id") or ""
        ).strip()
        if summary_camera and summary_camera != camera_id:
            raise ReplayError(
                f"summary camera_id {summary_camera!r} differs from events {camera_id!r}"
            )

    if validate:
        events = validate_events(events, camera_id=camera_id, run_id=effective_run_id)
    events = ensure_event_ids(
        events, run_id=effective_run_id, camera_id=camera_id
    )

    media_event_count = 0
    if media_manifest:
        merged: list[dict[str, Any]] = []
        for event in events:
            fields = media_manifest.get(str(event.get("event_id")))
            if fields:
                item = dict(event)
                for key, value in fields.items():
                    if value is not None:
                        item.setdefault(key, value)
                merged.append(item)
                media_event_count += 1
            else:
                merged.append(event)
        events = merged

    tracks: list[dict[str, Any]] | None = None
    if tracks_path:
        tracks = load_jsonl(tracks_path)
        if validate:
            tracks = validate_tracks(
                tracks, camera_id=camera_id, run_id=effective_run_id
            )

    batches = group_windows(
        events,
        window_seconds=window_seconds,
        max_batch=max_batch,
        start_at_first_event=start_at_first_event,
    )

    result = ReplayResult(
        run_id=effective_run_id,
        camera_id=camera_id,
        dry_run=dry_run,
        batches_dir=None,
        base_url=(url or "").strip(),
        media_events=media_event_count,
    )
    result.started_at = _utc_now()
    started = time.monotonic()

    if batches:
        first = batches[0].events[0]
        last = batches[-1].events[-1]
        first_time = event_offset(first) or 0.0
        last_time = event_offset(last) or first_time
        result.event_span_s = max(0.0, last_time - first_time)
    result.events_sent = len(events)
    result.event_batches = len(batches)
    result.tracks_sent = len(tracks or [])

    run_body: dict[str, Any] | None = None
    if summary is not None:
        run_meta = dict(summary.get("run") or {})
        run_meta.setdefault("run_id", effective_run_id)
        run_meta.setdefault("camera_id", camera_id)
        run_body = {
            "run_id": effective_run_id,
            "camera_id": camera_id,
            "source": envelope_source,
            "sent_at": _utc_now(),
            "complete": bool(complete),
            "run": run_meta,
            "summary": {key: value for key, value in summary.items() if key != "run"},
            "config": config or {},
        }

    if dry_run:
        target = Path(dry_run_dir or "output/replay") / effective_run_id
        target.mkdir(parents=True, exist_ok=True)
        for batch in batches:
            envelope = _envelope(
                effective_run_id, camera_id, "events", batch.events, envelope_source
            )
            (target / f"events_{batch.index:04d}.json").write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        for index, chunk in enumerate(_chunks(tracks or [], max_batch)):
            envelope = _envelope(
                effective_run_id, camera_id, "tracks", list(chunk), envelope_source
            )
            (target / f"tracks_{index:04d}.json").write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result.track_batches += 1
        if run_body is not None:
            (target / "run.json").write_text(
                json.dumps(run_body, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result.run_sent = True
        manifest = {
            "run_id": effective_run_id,
            "camera_id": camera_id,
            "source": envelope_source,
            "complete": bool(complete),
            "window_seconds": window_seconds,
            "speed": speed,
            "batches": len(batches),
            "events": len(events),
            "tracks": len(tracks or []),
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result.batches_dir = str(target)
        result.elapsed_s = time.monotonic() - started
        result.finished_at = _utc_now()
        return result

    if not url:
        raise ReplayError(
            "A pipeline webhook URL is required unless dry_run=True "
            f"(set PIPELINE_BASE_URL or pass {DEFAULT_BASE_URL})"
        )
    base_url = resolve_base_url(url)
    result.base_url = base_url
    client = session or requests.Session()
    headers = build_headers(token)

    if not skip_health_check:
        wait_healthy(
            base_url,
            token=token,
            attempts=health_attempts,
            delay=health_delay,
            timeout=min(timeout, 10.0),
            session=client,
            sleep=sleep,
        )

    schedule_start = time.monotonic()
    for batch in batches:
        target_offset = batch.offset_s / speed
        wait = target_offset - (time.monotonic() - schedule_start)
        if wait > 0:
            sleep(wait)
        envelope = _envelope(
            effective_run_id, camera_id, "events", batch.events, envelope_source
        )
        body, retry_count = _post_batch(
            client,
            endpoint(base_url, "/webhook"),
            envelope,
            headers=headers,
            timeout=timeout,
            retries=retries,
            sleep=sleep,
        )
        result.retries += retry_count
        result.duplicates += int(body.get("duplicates") or 0)
        result.accepted += int(body.get("accepted") or 0)
        log.info(
            "run %s: batch %d/%d (%d events)",
            effective_run_id,
            batch.index + 1,
            len(batches),
            len(batch.events),
        )

    for chunk in _chunks(tracks or [], max_batch):
        body, retry_count = _post_batch(
            client,
            endpoint(base_url, "/webhook/tracks"),
            _envelope(
                effective_run_id, camera_id, "tracks", list(chunk), envelope_source
            ),
            headers=headers,
            timeout=timeout,
            retries=retries,
            sleep=sleep,
        )
        result.retries += retry_count
        result.duplicates += int(body.get("duplicates") or 0)
        result.accepted += int(body.get("accepted") or 0)
        result.track_batches += 1

    if run_body is not None:
        body, retry_count = _post_batch(
            client,
            endpoint(base_url, "/webhook/run"),
            run_body,
            headers=headers,
            timeout=timeout,
            retries=retries,
            sleep=sleep,
        )
        result.retries += retry_count
        result.duplicates += int(body.get("duplicates") or 0)
        result.accepted += int(body.get("accepted") or 0)
        result.run_sent = True
    else:
        log.warning(
            "run %s: no summary provided, so /webhook/run was not sent; "
            "the dashboard will not treat this run as complete",
            effective_run_id,
        )

    result.elapsed_s = time.monotonic() - started
    result.finished_at = _utc_now()
    return result
