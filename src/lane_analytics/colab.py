"""One-call activation helper for Google Colab.

The notebook only needs to clone the repository and call :func:`activate`:

    from lane_analytics.colab import activate

    activate(source="drive_zip", drive_url="https://drive.google.com/...")

    activate(source="live", record_seconds=300)

The helper downloads or records the footage, runs the batch vision pipeline, and
replays the artifacts to the production pipeline through the Cloudflare Tunnel. The
token is read from the ``PIPELINE_WEBHOOK_TOKEN`` secret; the base URL defaults to
``https://lane-api.syaifudin-fam.my.id`` and can be overridden with
``PIPELINE_BASE_URL``. It never receives data.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import load_config
from .replay import DEFAULT_BASE_URL, DEFAULT_SOURCE, ReplayResult, replay_run
from .utils import ensure_dir

log = logging.getLogger(__name__)

DEFAULT_STREAM_URL = (
    "https://pub2.hk-opt.com/LiveApp/streams/501264524429471982550467.m3u8"
)
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".ts"}
_TIMESTAMP_PATTERNS = (
    re.compile(
        r"(?P<year>20\d{2})[-_]?(?P<month>\d{2})[-_]?(?P<day>\d{2})"
        r"[T _-]?(?P<hour>\d{2})[-_:]?(?P<minute>\d{2})[-_:]?(?P<second>\d{2})"
    ),
)


class ActivationError(RuntimeError):
    """Raised when activation cannot continue."""


@dataclass
class ActivationResult:
    source: str
    videos: list[dict[str, Any]] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return sum(1 for item in self.videos if item.get("status") == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.videos if item.get("status") == "error")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "processed": self.processed,
            "failed": self.failed,
            "videos": self.videos,
        }

    def __repr__(self) -> str:  # pragma: no cover - display helper
        return f"ActivationResult(source={self.source!r}, processed={self.processed}, failed={self.failed})"


# --------------------------------------------------------------- secrets
def _secret(name: str) -> str | None:
    """Read a value from the environment, Kaggle Secrets, or Colab Secrets."""
    value = os.getenv(name)
    if value:
        return value
    try:  # pragma: no cover - only available inside Kaggle
        from kaggle_secrets import UserSecretsClient  # type: ignore

        return UserSecretsClient().get_secret(name)
    except Exception:
        pass
    try:  # pragma: no cover - only available inside Colab
        from google.colab import userdata  # type: ignore

        return userdata.get(name)
    except Exception:
        return None


# --------------------------------------------------------------- source prep
def download_drive(url: str, destination: str | Path) -> list[Path]:
    """Download a public Google Drive file or folder with gdown."""
    try:
        import gdown  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ActivationError(
            "gdown is required to fetch the Google Drive archive. "
            "Install it with: pip install gdown"
        ) from exc

    target = ensure_dir(destination)
    file_id = _drive_file_id(url)
    is_folder = file_id is None
    if is_folder:
        log.info("Downloading Google Drive folder to %s", target)
        result = gdown.download_folder(str(url), output=str(target), quiet=False, use_cookies=False)
        return [Path(item) for item in (result or [])]

    log.info("Downloading Google Drive file %s to %s", file_id, target)
    output = Path(target) / "source.zip"
    downloaded = gdown.download(id=file_id, output=str(output), quiet=False)
    if not downloaded:
        raise ActivationError("gdown did not return a downloaded path")
    return [Path(downloaded)]


def _drive_file_id(url: str) -> str | None:
    patterns = (
        r"/file/d/([A-Za-z0-9_-]+)",
        r"[?&]id=([A-Za-z0-9_-]+)",
        r"/uc\?id=([A-Za-z0-9_-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", url.strip()):
        return url.strip()
    return None


def extract_zip(archive: str | Path, destination: str | Path) -> Path:
    """Extract a zip safely, skipping macOS metadata and zip-slip paths."""
    source = Path(archive)
    target = ensure_dir(destination)
    if not source.exists():
        raise ActivationError(f"Archive not found: {source}")
    if any(target.iterdir()):
        log.info("Reusing extracted archive at %s", target)
        return target

    with zipfile.ZipFile(source) as bundle:
        for info in bundle.infolist():
            name = info.filename
            if name.startswith("__MACOSX/") or name.endswith("/.DS_Store"):
                continue
            member = target / name
            resolved = member.resolve()
            if target.resolve() not in resolved.parents and resolved != target.resolve():
                raise ActivationError(f"Unsafe path inside archive: {name}")
            if info.is_dir():
                resolved.mkdir(parents=True, exist_ok=True)
                continue
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source_handle, resolved.open("wb") as dest_handle:
                shutil.copyfileobj(source_handle, dest_handle)
    return target


def discover_videos(root: str | Path) -> list[Path]:
    """Find video files recursively, excluding annotated outputs."""
    base = Path(root)
    if not base.exists():
        return []
    videos = [
        path
        for path in base.rglob("*")
        if path.is_file()
        and path.suffix.lower() in VIDEO_SUFFIXES
        and "_annotated" not in path.stem
    ]
    return sorted(videos, key=lambda path: str(path).lower())


def video_duration_s(path: str | Path) -> float:
    """Source duration in seconds; 300 s fallback when OpenCV cannot read it."""
    try:
        import cv2

        capture = cv2.VideoCapture(str(path))
        try:
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            frames = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        finally:
            capture.release()
        if fps > 0 and frames > 0:
            return frames / fps
    except Exception:  # pragma: no cover - defensive
        pass
    return 300.0


def infer_timestamp_from_name(path: str | Path) -> str | None:
    """Extract ``YYYYMMDD_HHMMSS`` (or ISO-like) from a filename."""
    stem = Path(path).stem
    for pattern in _TIMESTAMP_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        try:
            value = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour")),
                int(match.group("minute")),
                int(match.group("second")),
            )
        except ValueError:
            continue
        return value.isoformat(timespec="seconds")
    return None


def plan_base_timestamps(
    videos: Sequence[Path],
    *,
    base_timestamp: str | None = None,
    base_timestamps: dict[str, str] | None = None,
    duration_fn: Callable[[Path], float] = video_duration_s,
) -> dict[Path, str]:
    """Assign a base timestamp to every video.

    Priority: explicit per-file mapping, then a single start time advanced by each
    clip's duration (so 12 clips form one continuous timeline), then a timestamp
    parsed from the filename, then the file mtime.
    """
    mapping = base_timestamps or {}
    resolved: dict[Path, str] = {}

    if mapping:
        for video in videos:
            value = (
                mapping.get(video.name)
                or mapping.get(video.stem)
                or mapping.get(str(video))
            )
            if value:
                resolved[video] = value
        if len(resolved) == len(videos):
            return resolved

    if base_timestamp:
        current = datetime.fromisoformat(base_timestamp)
        for video in videos:
            resolved.setdefault(video, current.isoformat(timespec="seconds"))
            current += timedelta(seconds=duration_fn(video))
        return resolved

    for video in videos:
        if video in resolved:
            continue
        inferred = infer_timestamp_from_name(video)
        if inferred:
            log.warning(
                "base_timestamp for %s inferred from the filename (%s). "
                "Pass base_timestamps={...} if the CCTV overlay time differs.",
                video.name,
                inferred,
            )
            resolved[video] = inferred
            continue
        try:
            resolved[video] = datetime.fromtimestamp(video.stat().st_mtime).isoformat(
                timespec="seconds"
            )
            log.warning(
                "base_timestamp for %s taken from the file mtime; "
                "pass base_timestamps={...} for accurate event times.",
                video.name,
            )
        except OSError:
            resolved[video] = datetime.now().isoformat(timespec="seconds")
    return resolved


def unique_run_ids(videos: Sequence[Path]) -> dict[Path, str]:
    """Deterministic, filename-safe run ids; duplicates get a numeric suffix."""
    allowed = re.compile(r"[^A-Za-z0-9_-]+")
    used: dict[str, int] = {}
    result: dict[Path, str] = {}
    for video in videos:
        base = allowed.sub("_", video.stem).strip("_") or "run"
        count = used.get(base, 0) + 1
        used[base] = count
        result[video] = base if count == 1 else f"{base}_{count:02d}"
    return result


# --------------------------------------------------------------- live source
def _record_live(
    stream_url: str,
    destination: str | Path,
    seconds: float,
    *,
    chunk_seconds: float = 30.0,
) -> tuple[Path, str]:
    from .recorder import record_stream

    target_dir = ensure_dir(destination)
    started = datetime.now().replace(microsecond=0)
    output = target_dir / f"live_{started:%Y%m%d_%H%M%S}.mp4"
    log.info("Recording %s for %.0f s to %s", stream_url, seconds, output)
    result = record_stream(
        stream_url,
        output,
        seconds,
        chunk_seconds=chunk_seconds,
    )
    if not output.exists() or output.stat().st_size == 0:
        raise ActivationError(f"Recording produced no data: {output}")
    if not result.complete:
        log.warning(
            "Live recording is short by %.1f s; continuing with what was captured",
            result.shortfall_seconds,
        )
    return output, started.isoformat(timespec="seconds")


# --------------------------------------------------------------- activation
def _default_device() -> str:
    try:  # pragma: no cover - GPU detection depends on the runtime
        import torch

        return "0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _process_one(
    video: Path,
    *,
    run_id: str,
    base_timestamp: str,
    config_path: str | Path,
    device: str | None,
    imgsz: int | None,
    frame_stride: int | None,
    max_frames: int | None,
    write_video: bool | None,
    session_id: str = "",
    window_index: int | None = None,
    window_count: int | None = None,
):
    from .pipeline import process_video

    config = load_config(config_path)
    config.video.path = str(video)
    config.base_timestamp = base_timestamp
    if device:
        config.model.device = device
    if imgsz is not None:
        config.model.imgsz = imgsz
    if frame_stride is not None:
        config.video.frame_stride = frame_stride
    if max_frames is not None:
        config.video.max_frames = max_frames
    config.validate()
    render_video = bool(config.evidence.enabled) if write_video is None else bool(write_video)
    return process_video(
        config,
        write_video=render_video,
        generate_reports=True,
        run_id=run_id,
        session_id=session_id,
        window_index=window_index,
        window_count=window_count,
    )


def activate(
    source: str = "drive_zip",
    *,
    drive_url: str | None = None,
    zip_path: str | Path | None = None,
    video_dir: str | Path = "data/raw",
    max_videos: int | None = None,
    base_timestamp: str | None = None,
    base_timestamps: dict[str, str] | None = None,
    stream_url: str = DEFAULT_STREAM_URL,
    record_seconds: float = 300.0,
    config_path: str | Path = "config/config.yaml",
    device: str | None = None,
    imgsz: int | None = None,
    frame_stride: int | None = None,
    max_frames: int | None = None,
    write_video: bool | None = None,
    replay: bool = True,
    webhook_url: str | None = None,
    webhook_token: str | None = None,
    replay_source: str | None = None,
    speed: float = 1.0,
    window_seconds: float = 5.0,
    gap_seconds: float = 0.0,
    retries: int = 5,
    health_attempts: int = 6,
    health_delay: float = 10.0,
    dry_run: bool = False,
    skip_health_check: bool = False,
    session_id: str | None = None,
    window_index: int | None = None,
    window_count: int | None = None,
    run_id: str | None = None,
) -> ActivationResult:
    """Prepare footage, run the batch pipeline, and replay it to the webhook.

    Parameters are kept intentionally small so the Colab notebook stays short.
    ``source`` selects one of two inputs:

    ``"drive_zip"``
        Download ``drive_url`` (public Google Drive zip/folder) or use a local
        ``zip_path``, extract it, and process every clip inside.

    ``"live"``
        Record ``record_seconds`` of ``stream_url`` with the existing recorder,
        then process that recording.

    When ``replay`` is true, each run is posted to ``webhook_url`` (falling back
    to the ``PIPELINE_BASE_URL``/``WEBHOOK_URL`` secret and then the production
    Cloudflare hostname) with the ``PIPELINE_WEBHOOK_TOKEN`` secret, using
    :func:`lane_analytics.replay.replay_run`.

    ``write_video=None`` renders the annotated video when evidence is enabled, so the
    per-event snapshots and clips show the detection boxes. Pass ``False`` to skip it.
    """
    kind = str(source).strip().lower()
    if kind not in {"drive_zip", "live"}:
        raise ActivationError("source must be 'drive_zip' or 'live'")

    if device is None:
        device = _default_device()
    if not url_is_set(webhook_url):
        webhook_url = (
            _secret("PIPELINE_BASE_URL")
            or _secret("WEBHOOK_URL")
            or DEFAULT_BASE_URL
        )
    if not webhook_token:
        webhook_token = _secret("PIPELINE_WEBHOOK_TOKEN") or _secret("WEBHOOK_TOKEN")
    result = ActivationResult(source=kind)

    if kind == "drive_zip":
        if not drive_url:
            drive_url = _secret("DRIVE_ZIP_URL")
        if not drive_url and not zip_path:
            raise ActivationError(
                "drive_url (or zip_path) is required for source='drive_zip'. "
                "Pass the public Google Drive link or set the DRIVE_ZIP_URL secret."
            )

    if replay and not dry_run and not webhook_token:
        raise ActivationError(
            "replay=True needs a pipeline token. Add PIPELINE_WEBHOOK_TOKEN to "
            "Colab/Kaggle Secrets, or pass webhook_token=..."
        )

    if kind == "drive_zip":
        videos, base_map = _prepare_archive_source(
            drive_url=drive_url,
            zip_path=zip_path,
            video_dir=video_dir,
            base_timestamp=base_timestamp,
            base_timestamps=base_timestamps,
        )
    else:
        video, recorded_at = _record_live(stream_url, video_dir, record_seconds)
        videos = [video]
        # A caller-provided base timestamp keeps live event-time on WIB even
        # when the container clock is UTC.
        base_map = {video: base_timestamp or recorded_at}

    if max_videos is not None:
        videos = videos[: max(0, int(max_videos))]
    if not videos:
        raise ActivationError("No video files were found for the selected source")

    run_ids = unique_run_ids(videos)
    if run_id:
        if len(videos) != 1:
            raise ActivationError(
                "run_id is only supported when exactly one video is processed"
            )
        from .pipeline import _safe_run_id

        run_ids[videos[0]] = _safe_run_id(run_id)
    if replay and not webhook_url and not dry_run:
        raise ActivationError(
            "replay=True requires webhook_url (or the WEBHOOK_URL secret). "
            "Use replay=False to only run the batch pipeline."
        )

    print(f"Activation source : {kind}")
    print(f"Videos found      : {len(videos)}")
    print(f"Device            : {device}")
    print(f"Replay            : {'dry-run' if dry_run else ('yes' if replay else 'no')}")
    print()

    for index, video in enumerate(videos, start=1):
        effective_run_id = run_ids[video]
        base_ts = base_map.get(video) or (base_timestamp or "")
        item: dict[str, Any] = {
            "index": index,
            "video": str(video),
            "run_id": effective_run_id,
            "base_timestamp": base_ts,
            "session_id": session_id or "",
            "window_index": window_index,
            "window_count": window_count,
            "status": "pending",
        }
        try:
            print(
                f"[{index}/{len(videos)}] {video.name} -> run_id={effective_run_id}"
                + (f" session={session_id} window={window_index}/{window_count}" if session_id else "")
            )
            batch = _process_one(
                video,
                run_id=effective_run_id,
                base_timestamp=base_ts,
                config_path=config_path,
                device=device,
                imgsz=imgsz,
                frame_stride=frame_stride,
                max_frames=max_frames,
                write_video=write_video,
                session_id=session_id or "",
                window_index=window_index,
                window_count=window_count,
            )
            item["vehicles"] = batch.total_vehicles
            item["events"] = batch.total_events
            item["hogging"] = batch.hogging_events
            item["evidence_events"] = batch.evidence_events
            item["evidence_snapshots"] = batch.evidence_snapshots
            item["evidence_clips"] = batch.evidence_clips
            item["evidence_dir"] = batch.evidence_dir

            if replay:
                replay_result = replay_run(
                    batch.events_path,
                    tracks_path=batch.tracks_path,
                    summary_path=str(Path(batch.reports_dir or "") / "summary.json")
                    if batch.reports_dir
                    else None,
                    config_path=str(Path(batch.reports_dir or "") / "run_config.json")
                    if batch.reports_dir
                    else None,
                    url=webhook_url,
                    token=webhook_token,
                    run_id=effective_run_id,
                    source=replay_source or DEFAULT_SOURCE,
                    speed=speed,
                    window_seconds=window_seconds,
                    retries=retries,
                    health_attempts=health_attempts,
                    health_delay=health_delay,
                    dry_run=dry_run,
                    skip_health_check=skip_health_check,
                )
                item["replay"] = replay_result.as_dict()
            item["status"] = "ok"
        except Exception as exc:  # keep going with the remaining clips
            log.exception("Activation failed for %s", video)
            item["status"] = "error"
            item["error"] = f"{type(exc).__name__}: {exc}"
        result.videos.append(item)

        if gap_seconds and index < len(videos):
            import time

            time.sleep(gap_seconds)

    print()
    print(f"Selesai: {result.processed} berhasil, {result.failed} gagal")
    return result


def url_is_set(value: str | None) -> bool:
    return bool(value and str(value).strip())


def _prepare_archive_source(
    *,
    drive_url: str | None,
    zip_path: str | Path | None,
    video_dir: str | Path,
    base_timestamp: str | None,
    base_timestamps: dict[str, str] | None,
) -> tuple[list[Path], dict[Path, str]]:
    root = ensure_dir(video_dir)
    archive: Path | None = Path(zip_path) if zip_path else None
    search_root = root

    if archive is None:
        downloads = ensure_dir(root / "downloads")
        downloaded = download_drive(str(drive_url), downloads)
        archives = [path for path in downloaded if path.suffix.lower() == ".zip"]
        if archives:
            archive = archives[0]
        else:
            search_root = downloads

    if archive is not None:
        search_root = extract_zip(archive, root / "archive" / archive.stem)

    videos = discover_videos(search_root)
    if not videos and search_root != root:
        videos = discover_videos(root)
    if not videos:
        raise ActivationError(f"No video files found under {search_root}")
    base_map = plan_base_timestamps(
        videos,
        base_timestamp=base_timestamp,
        base_timestamps=base_timestamps,
    )
    return videos, base_map
