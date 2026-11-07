"""Aggregate lane events and track summaries into reports."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from .utils import ensure_dir, format_counts

EVENT_COLUMNS = [
    "camera_id",
    "timestamp",
    "track_id",
    "object_class",
    "confidence",
    "event_type",
    "lane_name",
    "lane_index",
    "lane_role",
    "previous_lane",
    "dwell_seconds",
    "class_id",
    "frame_index",
    "video_time_s",
    "severity",
    "rule_name",
    "message",
    "schema_version",
    "event_id",
]

VEHICLE_COLUMNS = [
    "camera_id",
    "track_id",
    "object_class",
    "class_id",
    "first_frame",
    "last_frame",
    "first_timestamp",
    "last_timestamp",
    "first_video_time_s",
    "last_video_time_s",
    "lane_change_count",
    "hogging_event_count",
    "dominant_lane",
    "total_lane_dwell_seconds",
]

STINT_COLUMNS = [
    "camera_id",
    "track_id",
    "object_class",
    "stint_id",
    "lane_name",
    "lane_index",
    "lane_role",
    "enter_frame",
    "enter_time_s",
    "enter_timestamp",
    "exit_frame",
    "exit_time_s",
    "exit_timestamp",
    "dwell_seconds",
    "hogging_indication",
]

HOGGING_COLUMNS = [
    "camera_id",
    "timestamp",
    "track_id",
    "object_class",
    "lane_name",
    "lane_index",
    "lane_role",
    "dwell_seconds",
    "severity",
    "rule_name",
    "message",
    "frame_index",
    "video_time_s",
    "event_id",
]


def load_events(path: str | Path) -> list[dict[str, Any]]:
    """Read an events JSONL (or CSV) file into a list of dictionaries."""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Events file not found: {source}")
    if source.suffix.lower() == ".csv":
        return pd.read_csv(source).fillna("").to_dict(orient="records")

    events: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number} of {source}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Line {line_number} of {source} is not a JSON object")
            events.append(record)
    return events


def load_tracks(path: str | Path | None) -> list[dict[str, Any]]:
    """Read a tracks JSONL file, returning an empty list when no path is given."""
    if not path:
        return []
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Tracks file not found: {source}")
    tracks: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number} of {source}: {exc}") from exc
            if isinstance(record, dict):
                tracks.append(record)
    return tracks


def _as_naive_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _floor_to_interval(value: datetime, interval_seconds: int) -> datetime:
    epoch = datetime(1970, 1, 1)
    total = int((value - epoch).total_seconds())
    return epoch + timedelta(seconds=(total // interval_seconds) * interval_seconds)


def _counts(mapping: Iterable[str]) -> dict[str, int]:
    counter = Counter(mapping)
    return {
        key: int(value)
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    }


def _stint_rows(tracks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for track in tracks:
        for stint in track.get("lane_stints") or []:
            rows.append(
                {
                    "camera_id": track.get("camera_id", ""),
                    "track_id": track.get("track_id"),
                    "object_class": track.get("object_class", "unknown"),
                    "stint_id": stint.get("stint_id"),
                    "lane_name": stint.get("lane_name", ""),
                    "lane_index": stint.get("lane_index"),
                    "lane_role": stint.get("lane_role", ""),
                    "enter_frame": stint.get("enter_frame"),
                    "enter_time_s": stint.get("enter_time_s"),
                    "enter_timestamp": stint.get("enter_timestamp", ""),
                    "exit_frame": stint.get("exit_frame"),
                    "exit_time_s": stint.get("exit_time_s"),
                    "exit_timestamp": stint.get("exit_timestamp", ""),
                    "dwell_seconds": float(stint.get("dwell_seconds") or 0.0),
                }
            )
    return rows


def _lane_usage(tracks: Sequence[dict[str, Any]], stint_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    total_vehicles = len({track.get("track_id") for track in tracks})
    total_dwell = sum(row["dwell_seconds"] for row in stint_rows)
    groups: dict[tuple[str, Any, str, str], dict[str, Any]] = {}
    for row in stint_rows:
        key = (row["lane_name"], row["lane_index"], row["lane_role"], row["object_class"])
        group = groups.setdefault(
            key,
            {
                "lane_name": row["lane_name"],
                "lane_index": row["lane_index"],
                "lane_role": row["lane_role"],
                "object_class": row["object_class"],
                "vehicle_ids": set(),
                "stint_count": 0,
                "total_dwell_s": 0.0,
                "max_dwell_s": 0.0,
            },
        )
        group["vehicle_ids"].add(row["track_id"])
        group["stint_count"] += 1
        group["total_dwell_s"] += row["dwell_seconds"]
        group["max_dwell_s"] = max(group["max_dwell_s"], row["dwell_seconds"])

    usage: list[dict[str, Any]] = []
    for group in groups.values():
        vehicles = len(group["vehicle_ids"])
        total = float(group["total_dwell_s"])
        usage.append(
            {
                "lane_name": group["lane_name"],
                "lane_index": group["lane_index"],
                "lane_role": group["lane_role"],
                "object_class": group["object_class"],
                "vehicle_count": vehicles,
                "stint_count": int(group["stint_count"]),
                "total_dwell_s": round(total, 3),
                "avg_dwell_s": round(total / group["stint_count"], 3)
                if group["stint_count"]
                else 0.0,
                "max_dwell_s": round(float(group["max_dwell_s"]), 3),
                "vehicle_share_pct": round(vehicles / total_vehicles * 100, 2)
                if total_vehicles
                else 0.0,
                "dwell_share_pct": round(total / total_dwell * 100, 2) if total_dwell else 0.0,
            }
        )
    usage.sort(
        key=lambda item: (
            item["lane_index"] if item["lane_index"] is not None else 10**6,
            item["lane_name"],
            item["object_class"],
        )
    )
    return usage


def summarize_events(
    events: Sequence[dict[str, Any]],
    tracks: Sequence[dict[str, Any]] | None = None,
    *,
    interval_seconds: int = 60,
    camera_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete lane-occupancy summary from events and track summaries."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    tracks = list(tracks or [])

    generated_at = datetime.now().replace(microsecond=0).isoformat()
    summary: dict[str, Any] = {
        "schema_version": "1.0",
        "camera_id": camera_id or "unknown",
        "generated_at": generated_at,
        "time_range": {"start": None, "end": None},
        "interval_seconds": interval_seconds,
        "total_vehicles": len(tracks),
        "total_events": 0,
        "vehicles_by_class": {},
        "events_by_type": {},
        "events_by_lane": {},
        "lane_usage": [],
        "lane_changes": {"total": 0, "by_class": {}, "pairs": []},
        "hogging": {
            "total": 0,
            "by_class": {},
            "by_severity": {},
            "by_rule": {},
            "by_lane": {},
            "events": [],
        },
        "intervals": [],
        "peak_interval": None,
        "run": meta or {},
    }
    if not tracks and not events:
        return summary

    if camera_id is None:
        cameras = Counter(
            str(item.get("camera_id") or "unknown") for item in list(events) + list(tracks)
        )
        if cameras:
            summary["camera_id"] = cameras.most_common(1)[0][0]

    vehicle_classes = Counter(str(track.get("object_class") or "unknown") for track in tracks)
    summary["vehicles_by_class"] = _counts(vehicle_classes.elements())

    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for index, event in enumerate(events):
        try:
            timestamp = _as_naive_datetime(str(event["timestamp"]))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid timestamp in event #{index}: {event.get('timestamp')!r}") from exc
        parsed.append((timestamp, event))

    event_types = [str(event.get("event_type") or "unknown") for _, event in parsed]
    event_lanes = [str(event.get("lane_name") or "unknown") for _, event in parsed]
    summary["events_by_type"] = _counts(event_types)
    summary["events_by_lane"] = _counts(event_lanes)

    stint_rows = _stint_rows(tracks)
    summary["lane_usage"] = _lane_usage(tracks, stint_rows)

    changes = [(ts, e) for ts, e in parsed if e.get("event_type") == "lane_change"]
    change_pairs: Counter[tuple[str, str]] = Counter(
        (str(event.get("previous_lane") or "unknown"), str(event.get("lane_name") or "unknown"))
        for _, event in changes
    )
    summary["lane_changes"] = {
        "total": len(changes),
        "by_class": _counts(str(event.get("object_class") or "unknown") for _, event in changes),
        "pairs": [
            {"from_lane": pair[0], "to_lane": pair[1], "count": int(count)}
            for pair, count in sorted(
                change_pairs.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }

    hogging = [(ts, e) for ts, e in parsed if e.get("event_type") == "lane_hogging_indication"]
    summary["hogging"] = {
        "total": len(hogging),
        "by_class": _counts(str(event.get("object_class") or "unknown") for _, event in hogging),
        "by_severity": _counts(str(event.get("severity") or "unknown") for _, event in hogging),
        "by_rule": _counts(str(event.get("rule_name") or "unknown") for _, event in hogging),
        "by_lane": _counts(str(event.get("lane_name") or "unknown") for _, event in hogging),
        "events": [
            {
                "timestamp": event.get("timestamp"),
                "track_id": event.get("track_id"),
                "object_class": event.get("object_class"),
                "lane_name": event.get("lane_name"),
                "lane_index": event.get("lane_index"),
                "lane_role": event.get("lane_role"),
                "dwell_seconds": event.get("dwell_seconds"),
                "severity": event.get("severity"),
                "rule_name": event.get("rule_name"),
                "message": event.get("message"),
                "frame_index": event.get("frame_index"),
                "video_time_s": event.get("video_time_s"),
                "event_id": event.get("event_id"),
            }
            for _, event in hogging
        ],
    }

    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for timestamp, event in parsed:
        bucket = _floor_to_interval(timestamp, interval_seconds)
        buckets.setdefault(bucket, []).append(event)
    intervals: list[dict[str, Any]] = []
    for bucket_start in sorted(buckets):
        bucket_events = buckets[bucket_start]
        intervals.append(
            {
                "interval_start": bucket_start.isoformat(timespec="seconds"),
                "interval_end": (bucket_start + timedelta(seconds=interval_seconds)).isoformat(
                    timespec="seconds"
                ),
                "count": len(bucket_events),
                "by_event_type": _counts(
                    str(event.get("event_type") or "unknown") for event in bucket_events
                ),
                "by_class": _counts(
                    str(event.get("object_class") or "unknown") for event in bucket_events
                ),
            }
        )

    summary.update(
        {
            "total_events": len(parsed),
            "time_range": {
                "start": min((ts for ts, _ in parsed), default=None),
                "end": max((ts for ts, _ in parsed), default=None),
            },
            "intervals": intervals,
            "peak_interval": max(intervals, key=lambda item: item["count"]) if intervals else None,
        }
    )
    if summary["time_range"]["start"] is not None:
        summary["time_range"] = {
            "start": summary["time_range"]["start"].isoformat(timespec="seconds"),
            "end": summary["time_range"]["end"].isoformat(timespec="seconds"),
        }
    return summary


def _empty_frame(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


def _hogging_stint_flags(
    events: Sequence[dict[str, Any]], stint_rows: Sequence[dict[str, Any]]
) -> set[int]:
    """Return indexes of stint rows that contain a hogging indication."""
    hogging = [event for event in events if event.get("event_type") == "lane_hogging_indication"]
    flagged: set[int] = set()
    for index, stint in enumerate(stint_rows):
        enter = float(stint.get("enter_time_s") or 0.0)
        exit_time = stint.get("exit_time_s")
        end = float(exit_time) if exit_time is not None else enter
        for event in hogging:
            if event.get("track_id") != stint.get("track_id"):
                continue
            if event.get("lane_name") != stint.get("lane_name"):
                continue
            when = float(event.get("video_time_s") or 0.0)
            if enter - 1e-6 <= when <= end + 1e-6:
                flagged.add(index)
                break
    return flagged


def write_reports(
    events: Sequence[dict[str, Any]],
    tracks: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    out_dir: str | Path,
    *,
    interval_seconds: int | None = None,
) -> dict[str, str]:
    """Write CSV, JSON, and Markdown artifacts."""
    target = ensure_dir(Path(out_dir))
    paths: dict[str, str] = {}

    events_frame = (
        pd.DataFrame(list(events), columns=EVENT_COLUMNS)
        if events
        else _empty_frame(EVENT_COLUMNS)
    )
    events_csv = target / "events.csv"
    events_frame.to_csv(events_csv, index=False)
    paths["events_csv"] = str(events_csv)

    vehicles_frame = (
        pd.DataFrame(list(tracks), columns=VEHICLE_COLUMNS)
        if tracks
        else _empty_frame(VEHICLE_COLUMNS)
    )
    vehicles_csv = target / "vehicles.csv"
    vehicles_frame.to_csv(vehicles_csv, index=False)
    paths["vehicles_csv"] = str(vehicles_csv)

    stint_rows = _stint_rows(tracks)
    flagged = _hogging_stint_flags(events, stint_rows)
    for index, row in enumerate(stint_rows):
        row["hogging_indication"] = index in flagged
    stints_frame = (
        pd.DataFrame(stint_rows, columns=STINT_COLUMNS)
        if stint_rows
        else _empty_frame(STINT_COLUMNS)
    )
    stints_csv = target / "lane_stints.csv"
    stints_frame.to_csv(stints_csv, index=False)
    paths["lane_stints_csv"] = str(stints_csv)

    usage_frame = (
        pd.DataFrame(summary.get("lane_usage") or [])
        if summary.get("lane_usage")
        else _empty_frame(
            [
                "lane_name",
                "lane_index",
                "lane_role",
                "object_class",
                "vehicle_count",
                "stint_count",
                "total_dwell_s",
                "avg_dwell_s",
                "max_dwell_s",
                "vehicle_share_pct",
                "dwell_share_pct",
            ]
        )
    )
    usage_csv = target / "lane_usage.csv"
    usage_frame.to_csv(usage_csv, index=False)
    paths["lane_usage_csv"] = str(usage_csv)

    change_events = [event for event in events if event.get("event_type") == "lane_change"]
    changes_frame = (
        pd.DataFrame(change_events, columns=EVENT_COLUMNS)
        if change_events
        else _empty_frame(EVENT_COLUMNS)
    )
    changes_csv = target / "lane_changes.csv"
    changes_frame.to_csv(changes_csv, index=False)
    paths["lane_changes_csv"] = str(changes_csv)

    hogging_events = [
        event for event in events if event.get("event_type") == "lane_hogging_indication"
    ]
    hogging_frame = (
        pd.DataFrame(hogging_events, columns=HOGGING_COLUMNS)
        if hogging_events
        else _empty_frame(HOGGING_COLUMNS)
    )
    hogging_csv = target / "hogging_events.csv"
    hogging_frame.to_csv(hogging_csv, index=False)
    paths["hogging_events_csv"] = str(hogging_csv)

    intervals = summary.get("intervals") or []
    interval_rows = [
        {
            "interval_start": item["interval_start"],
            "interval_end": item["interval_end"],
            "count": item["count"],
            "by_event_type": format_counts(item.get("by_event_type") or {}),
            "by_class": format_counts(item.get("by_class") or {}),
        }
        for item in intervals
    ]
    intervals_csv = target / "event_intervals.csv"
    pd.DataFrame(interval_rows).to_csv(intervals_csv, index=False)
    paths["event_intervals_csv"] = str(intervals_csv)

    summary_json = target / "summary.json"
    summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["summary_json"] = str(summary_json)

    summary_md = target / "summary.md"
    summary_md.write_text(render_markdown(summary), encoding="utf-8")
    paths["summary_md"] = str(summary_md)
    return paths


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def _seconds(value: Any) -> str:
    try:
        return f"{float(value):.1f}s"
    except (TypeError, ValueError):
        return "-"


def render_markdown(summary: dict[str, Any]) -> str:
    """Render a shareable Markdown report from a summary dictionary."""
    run = dict(summary.get("run") or {})
    total_vehicles = int(summary.get("total_vehicles") or 0)
    total_events = int(summary.get("total_events") or 0)
    time_range = summary.get("time_range") or {}
    lane_changes = summary.get("lane_changes") or {}
    hogging = summary.get("hogging") or {}

    lines = [
        "# Highway Lane Occupancy & Hogging Report",
        "",
        f"- **Camera:** `{summary.get('camera_id', 'unknown')}`",
        f"- **Generated:** {summary.get('generated_at', '-')}",
        f"- **Period:** {time_range.get('start') or '-'} to {time_range.get('end') or '-'}",
        f"- **Unique vehicles:** **{total_vehicles}**",
        f"- **Structured events:** **{total_events}**",
        f"- **Lane changes:** **{int(lane_changes.get('total') or 0)}**",
        f"- **Lane-hogging indications:** **{int(hogging.get('total') or 0)}**",
    ]
    if run.get("source_video"):
        lines.append(f"- **Source video:** `{run['source_video']}`")
    if run.get("frames_processed") is not None:
        lines.append(
            f"- **Frames processed:** {run.get('frames_processed')} "
            f"(stride {run.get('frame_stride', '-')}, {run.get('processing_fps', '-')} fps processing)"
        )
    lines.append("")

    lines.extend(["## Vehicles by class", ""])
    if summary.get("vehicles_by_class"):
        lines.append(
            _md_table(
                ["Class", "Vehicles", "Share"],
                [
                    [name, count, f"{count / total_vehicles * 100:.1f}%" if total_vehicles else "-"]
                    for name, count in (summary.get("vehicles_by_class") or {}).items()
                ],
            )
        )
    else:
        lines.append("No vehicle tracks were recorded.")
    lines.append("")

    lines.extend(["## Lane usage by class", ""])
    usage = summary.get("lane_usage") or []
    if usage:
        lines.append(
            _md_table(
                [
                    "Lane",
                    "Index",
                    "Role",
                    "Class",
                    "Vehicles",
                    "Stints",
                    "Total dwell",
                    "Avg dwell",
                    "Dwell share",
                ],
                [
                    [
                        row["lane_name"],
                        row.get("lane_index") if row.get("lane_index") is not None else "-",
                        row.get("lane_role") or "-",
                        row["object_class"],
                        row["vehicle_count"],
                        row["stint_count"],
                        _seconds(row["total_dwell_s"]),
                        _seconds(row["avg_dwell_s"]),
                        f"{row['dwell_share_pct']:.1f}%",
                    ]
                    for row in usage
                ],
            )
        )
    else:
        lines.append("No completed lane stints were recorded.")
    lines.append("")

    lines.extend(["## Lane changes", ""])
    if lane_changes.get("total"):
        lines.append(
            _md_table(
                ["Class", "Lane changes"],
                [[name, count] for name, count in (lane_changes.get("by_class") or {}).items()],
            )
        )
        if lane_changes.get("pairs"):
            lines.extend(["", "Most frequent transitions:", ""])
            lines.append(
                _md_table(
                    ["From", "To", "Count"],
                    [
                        [pair["from_lane"], pair["to_lane"], pair["count"]]
                        for pair in (lane_changes.get("pairs") or [])[:10]
                    ],
                )
            )
    else:
        lines.append("No lane changes were recorded.")
    lines.append("")

    lines.extend(["## Lane-hogging indications", ""])
    if hogging.get("total"):
        lines.append(
            _md_table(
                ["Timestamp", "Track", "Class", "Lane", "Dwell", "Severity", "Rule"],
                [
                    [
                        item.get("timestamp", "-"),
                        item.get("track_id", "-"),
                        item.get("object_class", "-"),
                        item.get("lane_name", "-"),
                        _seconds(item.get("dwell_seconds")),
                        item.get("severity", "-"),
                        item.get("rule_name", "-"),
                    ]
                    for item in (hogging.get("events") or [])
                ],
            )
        )
    else:
        lines.append("No lane-hogging indications matched the configured rules.")
    lines.append("")

    if summary.get("events_by_type"):
        lines.extend(["## Events by type", ""])
        lines.append(
            _md_table(
                ["Event type", "Count"],
                [[name, count] for name, count in summary["events_by_type"].items()],
            )
        )
        lines.append("")

    if run:
        lines.extend(["## Run metadata", ""])
        lines.append(
            _md_table(
                ["Key", "Value"],
                [[key, value] for key, value in sorted(run.items())],
            )
        )
        lines.append("")

    lines.extend(
        [
            "> Lane-hogging is a rule-based **indication** produced from class, lane, and",
            "> dwell time. It is not a legal determination. Review the configuration against",
            "> the actual road rules before using the output for enforcement.",
            "",
        ]
    )
    return "\n".join(lines)
