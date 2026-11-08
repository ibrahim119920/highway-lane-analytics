"""Parallel segmented processing with cross-segment track handover.

A long clip is split into overlapping segments that run in separate worker
processes. Each worker is a normal :func:`lane_analytics.pipeline.process_video`
run, so detection, ByteTrack, lane assignment, and evidence stay unchanged. The
only extra artifact is a frame log with the raw tracked boxes.

After the workers finish, tracks that survive an overlap are matched one-to-one
with a Hungarian assignment gated on spatial IoU and motion heading. A matched
track in the later segment inherits the earlier segment's canonical id, so the
final event/track stream has continuous ids across every seam. The merged
per-frame observations are then replayed through a single global tracker, which
rebuilds lane stints, dwell times, lane changes, and hogging indications without
duplicating events in the overlap windows.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import AppConfig
from .models import TrackedObject
from .pipeline import RunResult, _safe_run_id, process_video
from .utils import ensure_dir

log = logging.getLogger(__name__)

DEFAULT_SEGMENT_SECONDS = 12.0
DEFAULT_OVERLAP_SECONDS = 3.0
DEFAULT_WORKERS = 3

# Spatial gate: boxes from two independent ByteTrack runs on the same frames
# should overlap heavily. The motion gate only rejects clearly opposite headings.
DEFAULT_MIN_IOU = 0.7
DEFAULT_MIN_MOTION_SIMILARITY = 0.0
DEFAULT_MIN_COMMON_FRAMES = 1
DEFAULT_MIN_MOTION_PX = 2.0

_UNMATCHED_COST = 1.0
_FORBIDDEN_COST = 1_000_000.0

# Each worker process keeps one YOLO model alive and only resets the tracker
# between segments; loading the weights for every 12-second slice would erase
# most of the parallel speed-up.
_WORKER_DETECTOR: Any = None
_WORKER_DETECTOR_KEY: tuple[Any, ...] | None = None
_WORKER_THREADS: int | None = None


@dataclass(frozen=True)
class Segment:
    """A half-open frame window ``[start_frame, end_frame)``."""

    index: int
    start_frame: int
    end_frame: int
    segment_seconds: float
    overlap_seconds: float

    @property
    def length(self) -> int:
        return self.end_frame - self.start_frame

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "frames": self.length,
        }


@dataclass(frozen=True)
class FrameObservation:
    """One vehicle box for one global source frame."""

    frame_index: int
    timestamp: str
    video_time_s: float
    track_id: int
    class_id: int
    class_name: str
    confidence: float
    bbox: tuple[float, float, float, float]

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "class_id": self.class_id,
            "class_name": self.class_name,
            "confidence": round(self.confidence, 6),
            "bbox": [round(value, 3) for value in self.bbox],
        }


@dataclass(frozen=True)
class TrackMatch:
    """A handover between an earlier and a later segment-local track."""

    segment: int
    earlier_track_id: int
    later_track_id: int
    canonical_track_id: int
    mean_iou: float
    best_iou: float
    motion_similarity: float
    common_frames: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "handover",
            "segment": self.segment,
            "earlier_track_id": self.earlier_track_id,
            "later_track_id": self.later_track_id,
            "canonical_track_id": self.canonical_track_id,
            "mean_iou": round(self.mean_iou, 4),
            "best_iou": round(self.best_iou, 4),
            "motion_similarity": round(self.motion_similarity, 4),
            "common_frames": self.common_frames,
        }


@dataclass
class _PendingFrame:
    timestamp: str
    video_time_s: float
    objects: dict[int, FrameObservation] = field(default_factory=dict)


# --------------------------------------------------------------------------- plan
def _validate_settings(
    *,
    segment_seconds: float,
    overlap_seconds: float,
    workers: int,
) -> None:
    if float(segment_seconds) <= 0:
        raise ValueError("segment_seconds must be > 0")
    if float(overlap_seconds) < 0:
        raise ValueError("overlap_seconds must be >= 0")
    if float(overlap_seconds) >= float(segment_seconds):
        raise ValueError("overlap_seconds must be smaller than segment_seconds")
    if int(workers) < 1:
        raise ValueError("workers must be >= 1")


def plan_segments(
    total_frames: int,
    source_fps: float,
    *,
    segment_seconds: float = DEFAULT_SEGMENT_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
    start_frame: int = 0,
    max_frames: int | None = None,
) -> list[Segment]:
    """Split a frame range into continuous, overlapping segments.

    With the 12 s / 3 s defaults the starts are 0, 9, 18, 27, ... seconds, so a
    30 s block contains three segments and every seam shares 3 s of footage.
    """
    _validate_settings(
        segment_seconds=segment_seconds,
        overlap_seconds=overlap_seconds,
        workers=1,
    )
    if source_fps <= 0:
        raise ValueError("source_fps must be > 0")

    first = max(0, int(start_frame))
    last = max(first, int(total_frames))
    if max_frames is not None:
        last = min(last, first + max(0, int(max_frames)))
    if last <= first:
        return []

    segment_frames = max(1, int(round(float(segment_seconds) * source_fps)))
    overlap_frames = max(0, int(round(float(overlap_seconds) * source_fps)))
    if overlap_frames >= segment_frames:
        overlap_frames = max(0, segment_frames - 1)
    step = max(1, segment_frames - overlap_frames)

    segments: list[Segment] = []
    index = 0
    while True:
        start = first + index * step
        if start >= last:
            break
        # A tail shorter than the overlap is cheaper to absorb into the previous
        # segment than to launch another worker for a handful of frames.
        if segments and last - segments[-1].end_frame <= overlap_frames:
            previous = segments[-1]
            segments[-1] = Segment(
                index=previous.index,
                start_frame=previous.start_frame,
                end_frame=last,
                segment_seconds=float(segment_seconds),
                overlap_seconds=float(overlap_seconds),
            )
            break
        end = min(start + segment_frames, last)
        segments.append(
            Segment(
                index=index,
                start_frame=start,
                end_frame=end,
                segment_seconds=float(segment_seconds),
                overlap_seconds=float(overlap_seconds),
            )
        )
        if end >= last:
            break
        index += 1
    return segments


# ---------------------------------------------------------------------- geometry
def bbox_iou(
    first: Sequence[float] | tuple[float, float, float, float],
    second: Sequence[float] | tuple[float, float, float, float],
) -> float:
    """Intersection over union for two ``(x1, y1, x2, y2)`` boxes."""
    ax1, ay1, ax2, ay2 = (float(value) for value in first)
    bx1, by1, bx2, by2 = (float(value) for value in second)

    intersection_width = min(ax2, bx2) - max(ax1, bx1)
    intersection_height = min(ay2, by2) - max(ay1, by1)
    if intersection_width <= 0 or intersection_height <= 0:
        return 0.0
    intersection = intersection_width * intersection_height
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _motion_heading(
    observations: Sequence[FrameObservation],
) -> tuple[float, float, float]:
    """Return ``(dx, dy, magnitude)`` from the first to the last centre."""
    if len(observations) < 2:
        return 0.0, 0.0, 0.0
    ordered = sorted(observations, key=lambda item: item.frame_index)
    first_x, first_y = ordered[0].center
    last_x, last_y = ordered[-1].center
    dx = last_x - first_x
    dy = last_y - first_y
    return dx, dy, math.hypot(dx, dy)


def _heading_similarity(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
    *,
    min_motion_px: float = DEFAULT_MIN_MOTION_PX,
) -> float:
    """Cosine similarity, or 1.0 when either vehicle is effectively static."""
    _, _, first_magnitude = first
    _, _, second_magnitude = second
    if first_magnitude < min_motion_px or second_magnitude < min_motion_px:
        return 1.0
    dot = first[0] * second[0] + first[1] * second[1]
    cosine = dot / (first_magnitude * second_magnitude)
    return max(-1.0, min(1.0, cosine))


# ----------------------------------------------------------------- assignment
def hungarian_assignment(cost: Sequence[Sequence[float]]) -> list[int]:
    """Minimum-cost assignment for a rectangular cost matrix.

    Returns one column index per row (``-1`` when a row has no column because
    there are more rows than columns). Pure Python so the vision runtime does
    not depend on SciPy.
    """
    if not cost or not cost[0]:
        return []
    n = len(cost)
    m = len(cost[0])
    if n > m:
        # Transpose so the algorithm can assign every row.
        transposed = [[float(cost[row][column]) for row in range(n)] for column in range(m)]
        assignment = hungarian_assignment(transposed)
        result = [-1] * n
        for row, column in enumerate(assignment):
            if column >= 0:
                result[column] = row
        return result

    # Hungarian algorithm with potentials (1-indexed working arrays).
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for row in range(1, n + 1):
        p[0] = row
        column0 = 0
        min_value = [math.inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[column0] = True
            current_row = p[column0]
            delta = math.inf
            column1 = 0
            for column in range(1, m + 1):
                if used[column]:
                    continue
                current = float(cost[current_row - 1][column - 1]) - u[current_row] - v[column]
                if current < min_value[column]:
                    min_value[column] = current
                    way[column] = column0
                if min_value[column] < delta:
                    delta = min_value[column]
                    column1 = column
            for column in range(0, m + 1):
                if used[column]:
                    u[p[column]] += delta
                    v[column] -= delta
                else:
                    min_value[column] -= delta
            column0 = column1
            if p[column0] == 0:
                break
        while column0:
            column1 = way[column0]
            p[column0] = p[column1]
            column0 = column1

    assignment = [-1] * n
    for column in range(1, m + 1):
        if p[column]:
            assignment[p[column] - 1] = column - 1
    return assignment


def _overlap_only(
    tracks: dict[int, list[FrameObservation]],
    start_frame: int,
    end_frame: int,
) -> dict[int, list[FrameObservation]]:
    selected: dict[int, list[FrameObservation]] = {}
    for track_id, observations in tracks.items():
        inside = [
            observation
            for observation in observations
            if start_frame <= observation.frame_index < end_frame
        ]
        if inside:
            selected[track_id] = inside
    return selected


def match_overlap_tracks(
    earlier: dict[int, list[FrameObservation]],
    later: dict[int, list[FrameObservation]],
    *,
    overlap_start: int,
    overlap_end: int,
    min_iou: float = DEFAULT_MIN_IOU,
    min_motion_similarity: float = DEFAULT_MIN_MOTION_SIMILARITY,
    min_common_frames: int = DEFAULT_MIN_COMMON_FRAMES,
    min_motion_px: float = DEFAULT_MIN_MOTION_PX,
) -> list[tuple[int, int, float, float, float, int]]:
    """Match earlier/later tracks in an overlap window.

    Returns tuples of ``(earlier_id, later_id, mean_iou, best_iou,
    motion_similarity, common_frames)``.
    """
    early = _overlap_only(earlier, overlap_start, overlap_end)
    late = _overlap_only(later, overlap_start, overlap_end)
    early_ids = sorted(early)
    late_ids = sorted(late)
    if not early_ids or not late_ids:
        return []

    boxes_early = {
        track_id: {
            observation.frame_index: observation.bbox for observation in observations
        }
        for track_id, observations in early.items()
    }
    boxes_late = {
        track_id: {
            observation.frame_index: observation.bbox for observation in observations
        }
        for track_id, observations in late.items()
    }
    heading_early = {
        track_id: _motion_heading(observations) for track_id, observations in early.items()
    }
    heading_late = {
        track_id: _motion_heading(observations) for track_id, observations in late.items()
    }

    statistics: dict[tuple[int, int], tuple[float, float, float, int]] = {}
    for earlier_id in early_ids:
        for later_id in late_ids:
            common = sorted(
                set(boxes_early[earlier_id]) & set(boxes_late[later_id])
            )
            if len(common) < max(1, int(min_common_frames)):
                continue
            ious = [
                bbox_iou(boxes_early[earlier_id][frame], boxes_late[later_id][frame])
                for frame in common
            ]
            mean_iou = sum(ious) / len(ious)
            if mean_iou < min_iou:
                continue
            motion = _heading_similarity(
                heading_early[earlier_id],
                heading_late[later_id],
                min_motion_px=min_motion_px,
            )
            if motion < min_motion_similarity:
                continue
            statistics[(earlier_id, later_id)] = (
                mean_iou,
                max(ious),
                motion,
                len(common),
            )

    if not statistics:
        return []

    row_count = len(early_ids)
    column_count = len(late_ids)
    size = row_count + column_count
    cost = [[_FORBIDDEN_COST] * size for _ in range(size)]
    for row in range(row_count):
        for column in range(column_count):
            statistics_key = (early_ids[row], late_ids[column])
            if statistics_key not in statistics:
                continue
            mean_iou, _, motion, _ = statistics[statistics_key]
            motion_scaled = (motion + 1.0) / 2.0
            score = 0.75 * mean_iou + 0.25 * motion_scaled
            cost[row][column] = 1.0 - score
        for column in range(column_count, size):
            cost[row][column] = _UNMATCHED_COST
    for row in range(row_count, size):
        for column in range(column_count):
            cost[row][column] = _UNMATCHED_COST
        for column in range(column_count, size):
            cost[row][column] = 0.0

    assignment = hungarian_assignment(cost)
    matches: list[tuple[int, int, float, float, float, int]] = []
    for row, column in enumerate(assignment):
        if row >= row_count or column < 0 or column >= column_count:
            continue
        statistics_key = (early_ids[row], late_ids[column])
        if statistics_key not in statistics:
            continue
        if cost[row][column] >= _UNMATCHED_COST - 1e-9:
            continue
        mean_iou, best_iou, motion, common = statistics[statistics_key]
        matches.append(
            (early_ids[row], late_ids[column], mean_iou, best_iou, motion, common)
        )
    return matches


# --------------------------------------------------------------- canonical ids
def _first_frame(track: Sequence[FrameObservation]) -> int:
    return min(observation.frame_index for observation in track)


def assign_first_segment_ids(
    local_tracks: dict[int, list[FrameObservation]],
) -> tuple[dict[int, int], int]:
    """Give the first segment canonical ids in first-appearance order."""
    ordered = sorted(local_tracks.items(), key=lambda item: (_first_frame(item[1]), item[0]))
    mapping = {track_id: index + 1 for index, (track_id, _) in enumerate(ordered)}
    return mapping, len(mapping) + 1


def assign_next_segment_ids(
    *,
    previous_local_tracks: dict[int, list[FrameObservation]],
    current_local_tracks: dict[int, list[FrameObservation]],
    previous_mapping: dict[int, int],
    next_canonical_id: int,
    segment_index: int,
    overlap_start: int,
    overlap_end: int,
    min_iou: float = DEFAULT_MIN_IOU,
    min_motion_similarity: float = DEFAULT_MIN_MOTION_SIMILARITY,
    min_common_frames: int = DEFAULT_MIN_COMMON_FRAMES,
) -> tuple[dict[int, int], int, list[TrackMatch]]:
    """Map the current segment's local ids onto the canonical id space."""
    matches = match_overlap_tracks(
        previous_local_tracks,
        current_local_tracks,
        overlap_start=overlap_start,
        overlap_end=overlap_end,
        min_iou=min_iou,
        min_motion_similarity=min_motion_similarity,
        min_common_frames=min_common_frames,
    )

    mapping: dict[int, int] = {}
    handovers: list[TrackMatch] = []
    for earlier_id, later_id, mean_iou, best_iou, motion, common in matches:
        canonical = previous_mapping.get(earlier_id)
        if canonical is None:
            continue
        mapping[later_id] = canonical
        handovers.append(
            TrackMatch(
                segment=segment_index,
                earlier_track_id=earlier_id,
                later_track_id=later_id,
                canonical_track_id=canonical,
                mean_iou=mean_iou,
                best_iou=best_iou,
                motion_similarity=motion,
                common_frames=common,
            )
        )

    unmatched = [
        (track_id, observations)
        for track_id, observations in current_local_tracks.items()
        if track_id not in mapping
    ]
    for track_id, observations in sorted(
        unmatched, key=lambda item: (_first_frame(item[1]), item[0])
    ):
        mapping[track_id] = next_canonical_id
        next_canonical_id += 1
    return mapping, next_canonical_id, handovers


# ---------------------------------------------------------------- frame logs
def read_frame_log(path: str | Path) -> list[dict[str, Any]]:
    """Read a frame log produced by ``process_video(frame_log_path=...)``."""
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:  # pragma: no cover - defensive
                raise ValueError(
                    f"Invalid JSON on line {line_number} of {path}: {exc}"
                ) from exc
            if isinstance(record, dict):
                records.append(record)
    return records


def build_local_tracks(records: Iterable[dict[str, Any]]) -> dict[int, list[FrameObservation]]:
    """Group raw frame-log objects by their segment-local track id."""
    tracks: dict[int, list[FrameObservation]] = {}
    for record in records:
        frame_index = int(record["frame_index"])
        timestamp = str(record.get("timestamp") or "")
        video_time_s = float(record.get("video_time_s") or 0.0)
        for item in record.get("objects") or []:
            track_id = int(item["track_id"])
            observation = FrameObservation(
                frame_index=frame_index,
                timestamp=timestamp,
                video_time_s=video_time_s,
                track_id=track_id,
                class_id=int(item.get("class_id") or 0),
                class_name=str(item.get("class_name") or ""),
                confidence=float(item.get("confidence") or 0.0),
                bbox=tuple(float(value) for value in item["bbox"]),  # type: ignore[arg-type]
            )
            tracks.setdefault(track_id, []).append(observation)
    return tracks


class _FrameLogMerger:
    """Incrementally merge segment logs into one canonical frame-ordered JSONL.

    Frames are only flushed once no later segment can still observe them, so
    memory stays bounded to roughly one overlap window regardless of clip length.
    When two adjacent segments saw the same frame, the higher confidence box wins.
    """

    def __init__(self, output_path: str | Path, segments: Sequence[Segment]) -> None:
        self._segments = segments
        self._pending: dict[int, _PendingFrame] = {}
        self._target = Path(output_path)
        ensure_dir(self._target.parent)
        self._handle = self._target.open("w", encoding="utf-8")
        self.written = 0

    def add(
        self,
        position: int,
        records: Iterable[dict[str, Any]],
        mapping: dict[int, int],
    ) -> None:
        for record in records:
            frame_index = int(record["frame_index"])
            slot = self._pending.get(frame_index)
            if slot is None:
                slot = _PendingFrame(
                    timestamp=str(record.get("timestamp") or ""),
                    video_time_s=float(record.get("video_time_s") or 0.0),
                )
                self._pending[frame_index] = slot
            for item in record.get("objects") or []:
                local_id = int(item["track_id"])
                canonical_id = mapping.get(local_id)
                if canonical_id is None:
                    continue
                observation = FrameObservation(
                    frame_index=frame_index,
                    timestamp=slot.timestamp,
                    video_time_s=slot.video_time_s,
                    track_id=canonical_id,
                    class_id=int(item.get("class_id") or 0),
                    class_name=str(item.get("class_name") or ""),
                    confidence=float(item.get("confidence") or 0.0),
                    bbox=tuple(float(value) for value in item["bbox"]),  # type: ignore[arg-type]
                )
                existing = slot.objects.get(canonical_id)
                if existing is None or observation.confidence >= existing.confidence:
                    slot.objects[canonical_id] = observation

        next_start = (
            self._segments[position + 1].start_frame
            if position + 1 < len(self._segments)
            else None
        )
        if next_start is None:
            frames = sorted(self._pending)
        else:
            frames = sorted(frame for frame in self._pending if frame < next_start)
        self._flush(frames)

    def _flush(self, frames: Iterable[int]) -> None:
        for frame_index in frames:
            slot = self._pending.pop(frame_index)
            record = {
                "frame_index": frame_index,
                "timestamp": slot.timestamp,
                "video_time_s": round(slot.video_time_s, 6),
                "objects": [
                    slot.objects[track_id].to_dict() for track_id in sorted(slot.objects)
                ],
            }
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.written += 1

    def finish(self) -> int:
        self._flush(sorted(self._pending))
        self._handle.close()
        return self.written

    def __enter__(self) -> "_FrameLogMerger":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        if not self._handle.closed:
            self._handle.close()


def merge_segment_frame_logs(
    segment_records: Sequence[Sequence[dict[str, Any]]],
    segments: Sequence[Segment],
    mappings: Sequence[dict[int, int]],
    output_path: str | Path,
) -> int:
    """Merge all segment logs, returning the number of frames written."""
    with _FrameLogMerger(output_path, segments) as merger:
        for position, records in enumerate(segment_records):
            merger.add(position, records, mappings[position])
        return merger.finish()


class FrameLogObservationSource:
    """Replay a merged frame log as detector observations, in frame order."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = self.path.open("r", encoding="utf-8")
        self._next = self._read_next()

    def _read_next(self) -> dict[str, Any] | None:
        for raw in self._handle:
            line = raw.strip()
            if line:
                return json.loads(line)
        return None

    def __call__(self, frame_index: int) -> list[TrackedObject]:
        while self._next is not None and int(self._next["frame_index"]) < frame_index:
            self._next = self._read_next()
        if self._next is None or int(self._next["frame_index"]) != int(frame_index):
            return []
        record = self._next
        self._next = self._read_next()
        return [
            TrackedObject(
                track_id=int(item["track_id"]),
                class_id=int(item.get("class_id") or 0),
                class_name=str(item.get("class_name") or ""),
                confidence=float(item.get("confidence") or 0.0),
                bbox=tuple(float(value) for value in item["bbox"]),  # type: ignore[arg-type]
            )
            for item in record.get("objects") or []
        ]

    def close(self) -> None:
        self._handle.close()


# -------------------------------------------------------------------- workers
def _worker_detector(config: AppConfig):
    """Return a per-process detector, resetting the tracker between segments."""
    global _WORKER_DETECTOR, _WORKER_DETECTOR_KEY

    model = config.model
    key = (
        model.weights,
        model.device,
        int(model.imgsz),
        float(model.conf),
        float(model.iou),
        tuple(int(class_id) for class_id in model.classes),
        model.tracker,
    )
    if _WORKER_DETECTOR is None or _WORKER_DETECTOR_KEY != key:
        from .detector import YoloByteTrackDetector

        _WORKER_DETECTOR = YoloByteTrackDetector(
            weights=model.weights,
            device=model.device,
            imgsz=model.imgsz,
            conf=model.conf,
            iou=model.iou,
            classes=model.classes,
            tracker=model.tracker,
        )
        _WORKER_DETECTOR_KEY = key
    else:
        reset = getattr(_WORKER_DETECTOR, "reset", None)
        if callable(reset):
            reset()
    return _WORKER_DETECTOR


def _segment_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Process one segment in a worker process."""
    threads = max(1, int(payload.get("threads") or 1))
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ.setdefault(variable, str(threads))
    global _WORKER_THREADS
    if _WORKER_THREADS != threads:
        try:
            import torch  # noqa: PLC0415 - optional, only when a real detector runs

            torch.set_num_threads(threads)
        except Exception:  # pragma: no cover - torch is optional for the demo
            pass
        _WORKER_THREADS = threads

    config: AppConfig = payload["config"]
    detector = _worker_detector(config)
    started = time.perf_counter()
    result = process_video(
        config,
        detector=detector,
        video_path=payload["video_path"],
        events_path=payload["events_path"],
        tracks_path=payload["tracks_path"],
        write_video=False,
        generate_reports=False,
        run_id=payload["run_id"],
        start_frame=int(payload["start_frame"]),
        frame_log_path=payload["frame_log_path"],
        frame_limit=int(payload["end_frame"]) - int(payload["start_frame"]),
    )
    return {
        "index": int(payload["index"]),
        "start_frame": int(payload["start_frame"]),
        "end_frame": int(payload["end_frame"]),
        "frame_log_path": payload["frame_log_path"],
        "frames_processed": int(result.frames_processed),
        "frames_read": int(result.frames_read),
        "events": int(result.total_events),
        "vehicles": int(result.total_vehicles),
        "processing_s": round(time.perf_counter() - started, 3),
    }


def _write_id_table(
    path: Path,
    mappings: Sequence[dict[int, int]],
    handovers: Sequence[TrackMatch],
) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        for position, mapping in enumerate(mappings):
            for local_id in sorted(mapping):
                record = {
                    "record_type": "track_map",
                    "segment": position,
                    "local_track_id": local_id,
                    "canonical_track_id": mapping[local_id],
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        for handover in handovers:
            handle.write(json.dumps(handover.to_dict(), ensure_ascii=False) + "\n")


def _fallback(
    config: AppConfig,
    **kwargs: Any,
) -> RunResult:
    log.info("Parallel segmentation not applicable; running the single-pass pipeline")
    return process_video(config, **kwargs)


def process_video_parallel(
    config: AppConfig,
    *,
    video_path: str | Path | None = None,
    output_video: str | Path | None = None,
    events_path: str | Path | None = None,
    tracks_path: str | Path | None = None,
    reports_dir: str | Path | None = None,
    write_video: bool = True,
    generate_reports: bool = True,
    run_id: str | None = None,
    start_frame: int = 0,
    workers: int = DEFAULT_WORKERS,
    segment_seconds: float = DEFAULT_SEGMENT_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
    min_iou: float = DEFAULT_MIN_IOU,
    min_motion_similarity: float = DEFAULT_MIN_MOTION_SIMILARITY,
) -> RunResult:
    """Process a video in overlapping parallel segments and stitch the result."""
    _validate_settings(
        segment_seconds=segment_seconds,
        overlap_seconds=overlap_seconds,
        workers=workers,
    )

    import cv2  # noqa: PLC0415 - keep module import light

    source_path = Path(video_path or config.video.path)
    if not source_path.exists():
        raise FileNotFoundError(
            f"Video not found: {source_path}. Put a traffic clip in data/raw/ or pass --video."
        )

    run_id = _safe_run_id(run_id or f"{config.camera_id}_{datetime.now():%Y%m%dT%H%M%S}")
    output_root = Path(config.output.dir)

    capture = cv2.VideoCapture(str(source_path))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV could not open video: {source_path}")
    try:
        source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        capture.release()
    if not source_fps or source_fps != source_fps or source_fps <= 0:  # NaN-safe
        source_fps = 25.0

    first_frame = max(0, int(start_frame))
    if total_frames > 0:
        first_frame = min(first_frame, max(0, total_frames - 1))
    window_limit = config.video.max_frames
    if window_limit is not None:
        window_limit = max(1, int(window_limit))

    segments = (
        plan_segments(
            total_frames,
            source_fps,
            segment_seconds=segment_seconds,
            overlap_seconds=overlap_seconds,
            start_frame=first_frame,
            max_frames=window_limit,
        )
        if total_frames > 0
        else []
    )

    single_pass_kwargs = {
        "video_path": source_path,
        "output_video": output_video,
        "events_path": events_path,
        "tracks_path": tracks_path,
        "reports_dir": reports_dir,
        "write_video": write_video,
        "generate_reports": generate_reports,
        "run_id": run_id,
        "start_frame": first_frame,
    }
    if int(workers) <= 1 or len(segments) <= 1:
        return _fallback(config, **single_pass_kwargs)

    effective_workers = max(1, min(int(workers), len(segments)))

    if str(config.model.device).strip().lower() not in {"", "cpu", "-1"}:
        log.warning(
            "Parallel workers all use device %r; GPU memory and compute are shared, "
            "so throughput may not improve. Use --workers 1 for GPU runs.",
            config.model.device,
        )

    started = time.perf_counter()
    work_root = Path(tempfile.mkdtemp(prefix=f"lane_parallel_{run_id}_"))
    log.info(
        "Parallel: %d segment(s) of %.1fs with %.1fs overlap across %d worker(s)",
        len(segments),
        float(segment_seconds),
        float(overlap_seconds),
        effective_workers,
    )

    try:
        base_timestamp = config.base_timestamp or datetime.now().replace(
            microsecond=0
        ).isoformat()
        segment_config = copy.deepcopy(config)
        segment_config.base_timestamp = base_timestamp
        segment_config.evidence.enabled = False
        stitch_config = copy.deepcopy(config)
        stitch_config.base_timestamp = base_timestamp

        # PyTorch defaults to one thread per physical core. On SMT machines
        # os.cpu_count() reports logical CPUs, so aim for physical cores and
        # split them evenly; oversubscribing makes every worker slower.
        physical_cores = max(1, (os.cpu_count() or 2) // 2)
        threads = max(1, physical_cores // max(1, effective_workers))
        payloads: list[dict[str, Any]] = []
        for segment in segments:
            payloads.append(
                {
                    "index": segment.index,
                    "start_frame": segment.start_frame,
                    "end_frame": segment.end_frame,
                    "config": segment_config,
                    "video_path": str(source_path),
                    "events_path": str(work_root / f"seg_{segment.index:04d}_events.jsonl"),
                    "tracks_path": str(work_root / f"seg_{segment.index:04d}_tracks.jsonl"),
                    "frame_log_path": str(work_root / f"seg_{segment.index:04d}_frames.jsonl"),
                    "run_id": f"{run_id}_seg{segment.index:04d}",
                    "threads": threads,
                }
            )

        segment_results: list[dict[str, Any]] = []
        with ProcessPoolExecutor(max_workers=effective_workers) as executor:
            futures = [executor.submit(_segment_worker, payload) for payload in payloads]
            for future, payload in zip(futures, payloads):
                try:
                    segment_results.append(future.result())
                except Exception as exc:
                    raise RuntimeError(
                        f"Segment {payload['index']} "
                        f"(frames {payload['start_frame']}-{payload['end_frame']}) failed: {exc}"
                    ) from exc
        segment_results.sort(key=lambda item: item["index"])

        for item in segment_results:
            log.debug(
                "Parallel: segment %d (%d-%d) processed %d frames in %.1fs",
                item["index"],
                item["start_frame"],
                item["end_frame"],
                item["frames_processed"],
                item["processing_s"],
            )
        log.info(
            "Parallel: %d segments finished in %.1fs (%.1fs of segment work)",
            len(segment_results),
            time.perf_counter() - started,
            sum(float(item["processing_s"]) for item in segment_results),
        )

        mappings: list[dict[int, int]] = []
        handovers: list[TrackMatch] = []
        merged_log = work_root / "merged_frames.jsonl"
        next_id = 1
        previous_local_tracks: dict[int, list[FrameObservation]] | None = None
        with _FrameLogMerger(merged_log, segments) as merger:
            for position, item in enumerate(segment_results):
                records = read_frame_log(item["frame_log_path"])
                local_tracks = build_local_tracks(records)
                current_segment = segments[position]
                if position == 0:
                    mapping, next_id = assign_first_segment_ids(local_tracks)
                else:
                    previous_segment = segments[position - 1]
                    mapping, next_id, matched = assign_next_segment_ids(
                        previous_local_tracks=previous_local_tracks or {},
                        current_local_tracks=local_tracks,
                        previous_mapping=mappings[position - 1],
                        next_canonical_id=next_id,
                        segment_index=current_segment.index,
                        overlap_start=current_segment.start_frame,
                        overlap_end=min(
                            previous_segment.end_frame, current_segment.end_frame
                        ),
                        min_iou=min_iou,
                        min_motion_similarity=min_motion_similarity,
                    )
                    handovers.extend(matched)
                    log.info(
                        "Parallel: segment %d matched %d track(s) to earlier segments",
                        current_segment.index,
                        len(matched),
                    )
                mappings.append(mapping)
                merger.add(position, records, mapping)
                previous_local_tracks = local_tracks
            merged_frames = merger.finish()

        tracks_setting = tracks_path or config.output.tracks
        tracks_out_path = (
            Path(tracks_setting)
            if tracks_setting
            else output_root / "tracks" / f"{run_id}.jsonl"
        )
        id_table = tracks_out_path.with_name(
            f"{tracks_out_path.stem}_id_handovers.jsonl"
        )
        _write_id_table(id_table, mappings, handovers)

        source = FrameLogObservationSource(merged_log)
        parallel_meta = {
            "parallel": True,
            "segments": len(segments),
            "workers": effective_workers,
            "segment_seconds": float(segment_seconds),
            "overlap_seconds": float(overlap_seconds),
            "id_handovers": len(handovers),
            "id_map": str(id_table),
        }
        try:
            result = process_video(
                stitch_config,
                video_path=source_path,
                output_video=output_video,
                events_path=events_path,
                tracks_path=tracks_path,
                reports_dir=reports_dir,
                write_video=write_video,
                generate_reports=generate_reports,
                run_id=run_id,
                start_frame=first_frame,
                frame_limit=window_limit,
                observation_source=source,
                detector_label=f"{stitch_config.model.weights} + ByteTrack (parallel)",
                extra_meta=parallel_meta,
            )
        finally:
            source.close()

        wall_s = time.perf_counter() - started
        result.segments = len(segments)
        result.workers = effective_workers
        result.segment_seconds = float(segment_seconds)
        result.overlap_seconds = float(overlap_seconds)
        result.id_handovers = len(handovers)
        result.wall_s = wall_s
        result.processing_s = wall_s
        run_meta = result.summary.setdefault("run", {})
        run_meta["parallel"] = True
        run_meta["segments"] = len(segments)
        run_meta["workers"] = effective_workers
        run_meta["segment_seconds"] = float(segment_seconds)
        run_meta["overlap_seconds"] = float(overlap_seconds)
        run_meta["id_handovers"] = len(handovers)
        run_meta["id_map"] = str(id_table)
        run_meta["frames_in_merged_log"] = merged_frames
        run_meta["processing_seconds"] = round(wall_s, 3)
        run_meta["processing_fps"] = (
            round(result.frames_processed / wall_s, 2) if wall_s > 0 else 0.0
        )
        if generate_reports and result.reports_dir:
            from .analytics import write_reports

            write_reports(
                [event.to_dict() for event in result.events],
                result.tracks,
                result.summary,
                Path(result.reports_dir),
                interval_seconds=stitch_config.analytics.interval_seconds,
            )
        return result
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
