"""Snapshot + short-clip evidence for lane events.

For every selected event this module writes:

- a JPEG/PNG snapshot at the event frame,
- an 8-second MP4 clip centred on the event (default 4 s before + 4 s after),
- an ``index.json`` that maps each event to its media files.

Events that share a frame (a lane change is emitted as exit -> change -> enter) share one
snapshot and one clip, so the files are not duplicated.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import cv2

log = logging.getLogger(__name__)


class EvidenceError(RuntimeError):
    """Raised when evidence cannot be generated."""


@dataclass
class VideoInfo:
    path: Path
    fps: float
    width: int
    height: int
    frame_count: int
    duration_s: float


@dataclass
class EvidenceResult:
    run_id: str
    directory: str
    index_path: str = ""
    event_count: int = 0
    snapshot_count: int = 0
    clip_count: int = 0
    skipped_events: int = 0
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "directory": self.directory,
            "index_path": self.index_path,
            "event_count": self.event_count,
            "snapshot_count": self.snapshot_count,
            "clip_count": self.clip_count,
            "skipped_events": self.skipped_events,
            "elapsed_s": round(self.elapsed_s, 3),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------- helpers
def _load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        raise EvidenceError(f"Events file not found: {source}")
    records: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise EvidenceError(f"{source}:{number} is not valid JSON: {exc}") from exc
            if isinstance(value, dict):
                records.append(value)
    return records


def probe_video(path: str | Path) -> VideoInfo:
    source = Path(path)
    if not source.exists():
        raise EvidenceError(f"Video not found: {source}")
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise EvidenceError(f"OpenCV could not open video: {source}")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        capture.release()
    duration = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
    return VideoInfo(source, fps, width, height, frame_count, duration)


def select_events(
    events: Sequence[dict[str, Any]],
    event_types: Sequence[str] = (),
) -> list[dict[str, Any]]:
    allowed = {str(name) for name in event_types} if event_types else None
    selected: list[dict[str, Any]] = []
    for event in events:
        name = str(event.get("event_type") or "")
        if allowed is not None and name not in allowed:
            continue
        selected.append(event)
    return selected


def clamp_window(
    event_time_s: float,
    before_seconds: float,
    after_seconds: float,
    duration_s: float,
) -> tuple[float, float]:
    start = max(0.0, float(event_time_s) - float(before_seconds))
    end = float(event_time_s) + float(after_seconds)
    if duration_s > 0:
        end = min(float(duration_s), end)
    if end <= start:
        end = start + 0.1
    return start, end


def event_time_s(event: dict[str, Any], fps: float) -> float | None:
    value = event.get("video_time_s")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    frame = event.get("frame_index")
    if frame is not None and fps > 0:
        try:
            return float(frame) / fps
        except (TypeError, ValueError):
            pass
    return None


def event_frame(event: dict[str, Any], fps: float) -> int | None:
    frame = event.get("frame_index")
    if frame is not None:
        try:
            return int(frame)
        except (TypeError, ValueError):
            pass
    value = event_time_s(event, fps)
    if value is not None and fps > 0:
        return int(round(value * fps))
    return None


def _slug(value: int | float) -> str:
    if isinstance(value, int):
        return f"{value:06d}"
    return f"t{int(round(float(value) * 1000)):09d}"


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _snapshot_extension(fmt: str) -> str:
    value = str(fmt).lower()
    return "png" if value == "png" else "jpg"


def _write_snapshot(
    video: Path,
    time_s: float,
    frame_index: int | None,
    destination: Path,
    *,
    fmt: str,
    quality: int,
) -> bool:
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        return False
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, time_s) * 1000.0)
        ok, frame = capture.read()
        if (not ok or frame is None) and frame_index is not None:
            capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_index))
            ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _snapshot_extension(fmt) == "png":
        return bool(cv2.imwrite(str(destination), frame))
    return bool(
        cv2.imwrite(str(destination), frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    )


def _ffmpeg_exe() -> str | None:
    """Locate ffmpeg, preferring the system binary and then imageio's wheel.

    ``imageio-ffmpeg`` ships a static ffmpeg with libx264, so a Windows/Colab
    install without a system ffmpeg still produces browser-playable H.264 clips
    instead of falling back to OpenCV's MPEG-4 Part 2 writer.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # pragma: no cover - depends on the optional binary wheel
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _write_clip_ffmpeg(
    video: Path,
    start_s: float,
    end_s: float,
    destination: Path,
    *,
    codec: str,
    preset: str,
    crf: int,
    max_bitrate_kbps: int = 0,
) -> bool:
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.05, end_s - start_s)
    rate_args: list[str] = []
    if max_bitrate_kbps and max_bitrate_kbps > 0:
        rate = f"{int(max_bitrate_kbps)}k"
        rate_args = ["-maxrate", rate, "-bufsize", f"{int(max_bitrate_kbps) * 2}k"]
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, start_s):.3f}",
        "-i",
        str(video),
        "-t",
        f"{duration:.3f}",
        "-an",
        "-c:v",
        codec,
        "-preset",
        preset,
        "-crf",
        str(crf),
        *rate_args,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("ffmpeg clip failed for %s: %s", destination.name, result.stderr.strip()[:200])
        return False
    return destination.exists() and destination.stat().st_size > 0


def _write_clip_opencv(
    video: Path,
    start_s: float,
    end_s: float,
    destination: Path,
    *,
    source_fps: float,
    width: int,
    height: int,
) -> bool:
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        return False
    fps = float(capture.get(cv2.CAP_PROP_FPS) or source_fps or 10.0)
    frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or width or 0)
    frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or height or 0)
    if frame_width <= 0 or frame_height <= 0:
        capture.release()
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(destination),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (frame_width, frame_height),
    )
    if not writer.isOpened():
        capture.release()
        return False

    capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start_s) * 1000.0)
    frames_to_write = max(1, int(round(max(0.05, end_s - start_s) * fps)))
    written = 0
    try:
        while written < frames_to_write:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            writer.write(frame)
            written += 1
    finally:
        writer.release()
        capture.release()
    return written > 0 and destination.exists() and destination.stat().st_size > 0


def generate_evidence(
    *,
    run_id: str,
    events_path: str | Path,
    video_path: str | Path,
    output_dir: str | Path | None = None,
    camera_id: str = "",
    event_types: Sequence[str] = (),
    before_seconds: float = 4.0,
    after_seconds: float = 4.0,
    snapshot_format: str = "jpg",
    snapshot_quality: int = 92,
    clip_codec: str = "libx264",
    clip_preset: str = "veryfast",
    clip_crf: int = 23,
    clip_max_bitrate_kbps: int = 500,
    max_clips: int | None = None,
    time_offset_s: float = 0.0,
    use_ffmpeg: bool = True,
) -> EvidenceResult:
    """Build snapshot and clip evidence for the selected events of one run.

    ``time_offset_s`` is the source time of the first frame in ``video_path``. It is
    non-zero when the run started with ``--start-frame``; event timestamps stay in
    source time while media seeks must use the video's local time.
    """
    started = time.perf_counter()
    events_file = Path(events_path)
    video_file = Path(video_path)
    events = _load_jsonl(events_file)
    info = probe_video(video_file)

    root = Path(output_dir) if output_dir else Path("output") / "evidence" / run_id
    snapshots_dir = root / "snapshots"
    clips_dir = root / "clips"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)

    selected = select_events(events, event_types)
    result = EvidenceResult(
        run_id=run_id,
        directory=str(root),
        event_count=len(selected),
    )

    prepared: list[dict[str, Any]] = []
    snapshot_specs: dict[Any, tuple[float, int | None]] = {}
    clip_specs: dict[tuple[float, float], tuple[float, float, int | None]] = {}

    for event in selected:
        source_time = event_time_s(event, info.fps)
        if source_time is None:
            result.skipped_events += 1
            continue
        local_time = float(source_time) - float(time_offset_s)
        if local_time < -0.05 or (
            info.duration_s > 0 and local_time > info.duration_s + 0.5
        ):
            result.skipped_events += 1
            continue
        local_time = max(0.0, local_time)
        source_frame = event_frame(event, info.fps)
        local_frame = int(round(local_time * info.fps)) if info.fps > 0 else None
        start, end = clamp_window(
            local_time, before_seconds, after_seconds, info.duration_s
        )
        snapshot_key: Any = local_frame if local_frame is not None else round(local_time, 2)
        snapshot_specs.setdefault(snapshot_key, (local_time, local_frame))
        clip_key = (round(start, 3), round(end, 3))
        clip_specs.setdefault(clip_key, (start, end, local_frame))
        prepared.append(
            {
                "event": event,
                "source_time": source_time,
                "source_frame": source_frame,
                "local_time": local_time,
                "local_frame": local_frame,
                "start": start,
                "end": end,
                "snapshot_key": snapshot_key,
                "clip_key": clip_key,
            }
        )

    extension = _snapshot_extension(snapshot_format)
    snapshot_paths: dict[Any, Path] = {}
    for key, (time_s, frame) in snapshot_specs.items():
        destination = snapshots_dir / f"snap_{_slug(key)}.{extension}"
        if _write_snapshot(
            video_file,
            time_s,
            frame,
            destination,
            fmt=snapshot_format,
            quality=snapshot_quality,
        ):
            snapshot_paths[key] = destination
        else:
            result.errors.append(f"snapshot failed at {time_s:.2f}s (frame {frame})")

    clip_paths: dict[tuple[float, float], Path] = {}
    emitted = 0
    if use_ffmpeg and _ffmpeg_exe() is None:
        log.warning(
            "ffmpeg is not available; clips fall back to OpenCV MPEG-4 Part 2, "
            "which some browsers cannot play. Install ffmpeg or imageio-ffmpeg "
            "for H.264 clips."
        )
    for key, (start, end, local_frame) in clip_specs.items():
        if max_clips is not None and emitted >= int(max_clips):
            break
        destination = clips_dir / f"clip_{_slug(local_frame if local_frame is not None else start)}.mp4"
        success = False
        if use_ffmpeg:
            success = _write_clip_ffmpeg(
                video_file,
                start,
                end,
                destination,
                codec=clip_codec,
                preset=clip_preset,
                crf=clip_crf,
                max_bitrate_kbps=clip_max_bitrate_kbps,
            )
        if not success:
            success = _write_clip_opencv(
                video_file,
                start,
                end,
                destination,
                source_fps=info.fps,
                width=info.width,
                height=info.height,
            )
        if success:
            clip_paths[key] = destination
            emitted += 1
        else:
            result.errors.append(f"clip failed at {start:.2f}-{end:.2f}s")

    entries: list[dict[str, Any]] = []
    for item in prepared:
        event = item["event"]
        snapshot = snapshot_paths.get(item["snapshot_key"])
        clip = clip_paths.get(item["clip_key"])
        entries.append(
            {
                "event_id": event.get("event_id", ""),
                "event_type": event.get("event_type", ""),
                "track_id": event.get("track_id"),
                "object_class": event.get("object_class", ""),
                "lane_name": event.get("lane_name", ""),
                "timestamp": event.get("timestamp", ""),
                "video_time_s": round(float(item["source_time"]), 3),
                "frame_index": item["source_frame"],
                "severity": event.get("severity", ""),
                "rule_name": event.get("rule_name", ""),
                "snapshot": _relative(snapshot, root) if snapshot else None,
                "clip": _relative(clip, root) if clip else None,
                "clip_start_s": round(float(item["start"]) + float(time_offset_s), 3),
                "clip_end_s": round(float(item["end"]) + float(time_offset_s), 3),
                "clip_local_start_s": round(float(item["start"]), 3),
                "clip_local_end_s": round(float(item["end"]), 3),
                "clip_duration_s": round(float(item["end"]) - float(item["start"]), 3),
            }
        )
    entries.sort(key=lambda entry: (entry["video_time_s"], str(entry["event_type"])))

    result.snapshot_count = len(snapshot_paths)
    result.clip_count = len(clip_paths)
    result.elapsed_s = time.perf_counter() - started
    result.index_path = str(root / "index.json")

    index = {
        "schema_version": "1.0",
        "run_id": run_id,
        "camera_id": camera_id,
        "source_video": str(video_file),
        "video": {
            "fps": round(info.fps, 3),
            "width": info.width,
            "height": info.height,
            "duration_s": round(info.duration_s, 3),
            "time_offset_s": round(float(time_offset_s), 3),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "before_seconds": before_seconds,
        "after_seconds": after_seconds,
        "event_types": list(event_types) if event_types else "all",
        "event_count": len(entries),
        "snapshot_count": result.snapshot_count,
        "clip_count": result.clip_count,
        "events": entries,
    }
    Path(result.index_path).write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
