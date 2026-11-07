"""Replay precomputed lane events to a webhook to simulate a live feed.

This module is the *sender* side of the handoff. It never receives data and it has
no knowledge of Kafka. It reads the batch artifacts produced by ``analyze`` and
posts them as micro-batches to an HTTP endpoint (normally exposed through ngrok).
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
RETRY_STATUS = {429, 500, 502, 503, 504}
ENVELOPE_SOURCE = "colab-replay"


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
    events_sent: int = 0
    event_batches: int = 0
    tracks_sent: int = 0
    track_batches: int = 0
    run_sent: bool = False
    retries: int = 0
    duplicates: int = 0
    elapsed_s: float = 0.0
    event_span_s: float = 0.0
    dry_run: bool = False
    started_at: str = ""
    finished_at: str = ""
    batches_dir: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "events_sent": self.events_sent,
            "event_batches": self.event_batches,
            "tracks_sent": self.tracks_sent,
            "track_batches": self.track_batches,
            "run_sent": self.run_sent,
            "retries": self.retries,
            "duplicates": self.duplicates,
            "elapsed_s": round(self.elapsed_s, 3),
            "event_span_s": round(self.event_span_s, 3),
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "batches_dir": self.batches_dir,
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


def check_health(
    base: str,
    *,
    token: str | None = None,
    timeout: float = 5.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    client = session or requests.Session()
    url = endpoint(base, "/health")
    try:
        response = client.get(url, headers=build_headers(token), timeout=timeout)
    except requests.RequestException as exc:
        raise ReplayError(
            f"Webhook is not reachable at {url}. Is the local receiver running?\n{exc}"
        ) from exc
    if response.status_code != 200:
        raise ReplayError(
            f"Webhook health check failed ({response.status_code}): {_response_text(response)}"
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:200]}
    log.info("Webhook healthy: %s", payload)
    return payload


# --------------------------------------------------------------- replay
def _envelope(
    run_id: str,
    camera_id: str,
    key: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "camera_id": camera_id,
        "source": ENVELOPE_SOURCE,
        "sent_at": _utc_now(),
        key: items,
    }


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def replay_run(
    events_path: str | Path,
    *,
    tracks_path: str | Path | None = None,
    summary_path: str | Path | None = None,
    config_path: str | Path | None = None,
    url: str | None = None,
    token: str | None = None,
    run_id: str | None = None,
    speed: float = 1.0,
    window_seconds: float = 5.0,
    max_batch: int = 200,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 5,
    start_at_first_event: bool = True,
    skip_health_check: bool = False,
    dry_run: bool = False,
    dry_run_dir: str | Path | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ReplayResult:
    """Replay one run's events (and optionally tracks/run metadata) to a webhook."""
    if speed <= 0:
        raise ReplayError("speed must be greater than zero")

    events_file = Path(events_path)
    events = load_jsonl(events_file)
    effective_run_id = run_id or events_file.stem
    camera_id = ""
    for event in events:
        if event.get("camera_id"):
            camera_id = str(event["camera_id"])
            break
    if not camera_id:
        raise ReplayError("events do not contain a camera_id")

    batches = group_windows(
        events,
        window_seconds=window_seconds,
        max_batch=max_batch,
        start_at_first_event=start_at_first_event,
    )

    result = ReplayResult(run_id=effective_run_id, dry_run=dry_run)
    result.started_at = _utc_now()
    started = time.monotonic()

    if batches:
        last = batches[-1].events[-1]
        first = batches[0].events[0]
        first_time = event_offset(first) or 0.0
        last_time = event_offset(last) or first_time
        result.event_span_s = max(0.0, last_time - (event_offset(batches[0].events[0]) or 0.0))
    result.events_sent = len(events)
    result.event_batches = len(batches)

    if dry_run:
        target = Path(dry_run_dir or "output/replay")
        target = target / effective_run_id
        target.mkdir(parents=True, exist_ok=True)
        for batch in batches:
            envelope = _envelope(effective_run_id, camera_id, "events", batch.events)
            (target / f"events_{batch.index:04d}.json").write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if tracks_path:
            tracks = load_jsonl(tracks_path)
            result.tracks_sent = len(tracks)
            result.track_batches = 0
            for index, chunk in enumerate(_chunks(tracks, max_batch)):
                envelope = _envelope(effective_run_id, camera_id, "tracks", list(chunk))
                (target / f"tracks_{index:04d}.json").write_text(
                    json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                result.track_batches += 1
        if summary_path:
            summary = load_summary(summary_path)
            run_meta = summary.get("run") or {}
            body = {
                "run_id": effective_run_id,
                "camera_id": camera_id,
                "source": ENVELOPE_SOURCE,
                "complete": True,
                "run": run_meta,
                "summary": {key: value for key, value in summary.items() if key != "run"},
            }
            if config_path:
                body["config"] = load_summary(config_path)
            (target / "run.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result.run_sent = True
        manifest = {
            "run_id": effective_run_id,
            "camera_id": camera_id,
            "window_seconds": window_seconds,
            "speed": speed,
            "batches": len(batches),
            "events": len(events),
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result.batches_dir = str(target)
        result.elapsed_s = time.monotonic() - started
        result.finished_at = _utc_now()
        return result

    if not url:
        raise ReplayError("A webhook URL is required unless dry_run=True")
    client = session or requests.Session()
    headers = build_headers(token)

    if not skip_health_check:
        check_health(url, token=token, session=client, timeout=min(timeout, 10.0))

    schedule_start = time.monotonic()
    for batch in batches:
        target_offset = batch.offset_s / speed
        wait = target_offset - (time.monotonic() - schedule_start)
        if wait > 0:
            sleep(wait)
        envelope = _envelope(effective_run_id, camera_id, "events", batch.events)
        response, retry_count = post_json(
            client,
            endpoint(url, "/webhook"),
            envelope,
            headers=headers,
            timeout=timeout,
            retries=retries,
            sleep=sleep,
        )
        result.retries += retry_count
        try:
            body = response.json()
        except ValueError:
            body = {}
        result.duplicates += int(body.get("duplicates") or 0)
        log.info(
            "run %s: batch %d/%d (%d events)",
            effective_run_id,
            batch.index + 1,
            len(batches),
            len(batch.events),
        )

    if tracks_path:
        tracks = load_jsonl(tracks_path)
        result.tracks_sent = len(tracks)
        for chunk in _chunks(tracks, max_batch):
            _, retry_count = post_json(
                client,
                endpoint(url, "/webhook/tracks"),
                _envelope(effective_run_id, camera_id, "tracks", list(chunk)),
                headers=headers,
                timeout=timeout,
                retries=retries,
                sleep=sleep,
            )
            result.retries += retry_count
            result.track_batches += 1

    if summary_path:
        summary = load_summary(summary_path)
        run_meta = summary.get("run") or {}
        body: dict[str, Any] = {
            "run_id": effective_run_id,
            "camera_id": camera_id,
            "source": ENVELOPE_SOURCE,
            "complete": True,
            "run": run_meta,
            "summary": {key: value for key, value in summary.items() if key != "run"},
        }
        if config_path:
            body["config"] = load_summary(config_path)
        _, retry_count = post_json(
            client,
            endpoint(url, "/webhook/run"),
            body,
            headers=headers,
            timeout=timeout,
            retries=retries,
            sleep=sleep,
        )
        result.retries += retry_count
        result.run_sent = True

    result.elapsed_s = time.monotonic() - started
    result.finished_at = _utc_now()
    return result
