"""Small shared helpers: timestamps, durations, logging, directories."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp such as ``2026-11-06T08:30:15``."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from exc


def timestamp_for_frame(base: datetime, frame_index: int, fps: float) -> str:
    """Map a source frame index to an ISO-8601 wall-clock timestamp."""
    seconds = frame_index / fps if fps > 0 else 0.0
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {sec:04.1f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes):02d}m {sec:04.1f}s"


def format_counts(counts: dict[str, int], limit: int = 8) -> str:
    """Render a count mapping as ``car=12, truck=4`` (largest first)."""
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return ", ".join(f"{key}={value}" for key, value in items) or "-"


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
