"""Copy the robust stream recorder from the traffic-video-analytics project."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

DEFAULT_CHUNK_SECONDS = 30.0
DEFAULT_STALL_TIMEOUT = 20.0
DEFAULT_MAX_ATTEMPTS = 30
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
MIN_CHUNK_SECONDS = 1.0
COMPLETE_RATIO = 0.98
MP4_SUFFIXES = {".mp4", ".m4v", ".mov"}

INSTALL_HINT = (
    "ffmpeg/ffprobe not found on PATH. Install ffmpeg "
    "(Linux: 'apt-get install -y ffmpeg', macOS: 'brew install ffmpeg', "
    "Windows: https://ffmpeg.org/download.html) or run this on Colab, "
    "which ships ffmpeg."
)


class RecorderError(RuntimeError):
    """Raised when a stream cannot be recorded at all."""


@dataclass
class RecordResult:
    output: str
    requested_seconds: float
    actual_seconds: float
    attempts: int
    chunk_count: int
    reencoded: bool = False
    chunk_durations: list[float] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.actual_seconds >= self.requested_seconds * COMPLETE_RATIO

    @property
    def shortfall_seconds(self) -> float:
        return max(0.0, self.requested_seconds - self.actual_seconds)


def _which(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise RecorderError(INSTALL_HINT)
    return path


def probe_duration(path: str | Path, *, ffprobe: str | None = None) -> float:
    """Return the container duration in seconds (0.0 when it cannot be read)."""
    ffprobe = ffprobe or shutil.which("ffprobe")
    if not ffprobe:
        raise RecorderError(INSTALL_HINT)

    candidate = Path(path)
    if not candidate.exists() or candidate.stat().st_size == 0:
        return 0.0

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(candidate),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    try:
        return max(0.0, float(lines[-1]))
    except (ValueError, IndexError):
        return 0.0


def _last_error(stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else "unknown ffmpeg error"


def _is_http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _chunk_command(
    ffmpeg: str,
    url: str,
    path: Path,
    seconds: float,
    *,
    stall_timeout: float,
    copy_stream: bool,
) -> list[str]:
    command = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
    if _is_http(url):
        command += [
            "-rw_timeout",
            str(int(stall_timeout * 1_000_000)),
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-live_start_index",
            "0",
        ]
    command += [
        "-fflags",
        "+genpts",
        "-i",
        url,
        "-t",
        f"{seconds:.3f}",
    ]
    if copy_stream:
        command += ["-c", "copy"]
    else:
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac"]
    command += ["-f", "mpegts", str(path)]
    return command


def _run_ffmpeg(command: Sequence[str], *, timeout: float) -> tuple[int | None, str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return completed.returncode, (completed.stderr or "").strip()
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return None, f"stalled for more than {timeout:.0f}s; {stderr.strip()}".strip()
    except OSError as exc:
        raise RecorderError(f"Could not run ffmpeg: {exc}") from exc


def _record_chunk(
    url: str,
    path: Path,
    seconds: float,
    *,
    ffmpeg: str,
    ffprobe: str,
    stall_timeout: float,
    reencode_fallback: bool,
) -> tuple[float, str]:
    strategies = [True, False] if reencode_fallback else [True]
    last_error = ""
    for copy_stream in strategies:
        command = _chunk_command(
            ffmpeg,
            url,
            path,
            seconds,
            stall_timeout=stall_timeout,
            copy_stream=copy_stream,
        )
        returncode, stderr = _run_ffmpeg(command, timeout=seconds + stall_timeout + 30.0)
        captured = probe_duration(path, ffprobe=ffprobe)
        if captured >= MIN_CHUNK_SECONDS:
            if returncode not in (0, None):
                log.debug("Chunk ended with code %s but kept %.1fs", returncode, captured)
            return captured, stderr
        last_error = stderr or f"exit code {returncode}"
        if path.exists():
            path.unlink()
    return 0.0, last_error


def _write_concat_list(chunks: Sequence[Path], list_path: Path) -> None:
    lines = []
    for chunk in chunks:
        escaped = chunk.as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mux_arguments(output: Path) -> list[str]:
    arguments = ["-map", "0:v:0", "-map", "0:a?"]
    if output.suffix.lower() in MP4_SUFFIXES:
        arguments += ["-movflags", "+faststart"]
    return arguments


def _finalize(
    chunks: Sequence[Path],
    output: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    work_dir: Path,
    reencode_fallback: bool,
) -> tuple[float, bool]:
    list_path = work_dir / "chunks.txt"
    _write_concat_list(chunks, list_path)
    concat_input = ["-f", "concat", "-safe", "0", "-fflags", "+genpts", "-i", str(list_path)]
    partial = output.with_name(f"{output.stem}.partial{output.suffix}")

    command = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
    command += concat_input + ["-c", "copy"] + _mux_arguments(output) + [str(partial)]
    returncode, stderr = _run_ffmpeg(command, timeout=600)
    duration = probe_duration(partial, ffprobe=ffprobe)

    if duration <= 0 and reencode_fallback:
        log.warning("Stream copy concat failed (%s); re-encoding", _last_error(stderr))
        command = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
        command += concat_input
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ]
        command += _mux_arguments(output) + [str(partial)]
        returncode, stderr = _run_ffmpeg(command, timeout=3600)
        duration = probe_duration(partial, ffprobe=ffprobe)
        if duration <= 0:
            if partial.exists():
                partial.unlink()
            raise RecorderError(
                "ffmpeg could not mux the recorded chunks into "
                f"{output.name}: {_last_error(stderr)}"
            )
        partial.replace(output)
        return duration, True

    if duration <= 0:
        if partial.exists():
            partial.unlink()
        raise RecorderError(
            f"ffmpeg could not mux the recorded chunks into {output.name}: {_last_error(stderr)}"
        )

    partial.replace(output)
    return duration, False


def record_stream(
    url: str,
    output: str | Path,
    duration: float,
    *,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    stall_timeout: float = DEFAULT_STALL_TIMEOUT,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    reencode_fallback: bool = True,
    overwrite: bool = True,
) -> RecordResult:
    """Record at least ``duration`` seconds of a live HLS/HTTP stream.

    The stream is captured in ``chunk_seconds`` slices. A dropped connection
    costs at most the rest of the current slice; the next attempt reconnects
    and keeps adding footage until the requested length is reached.
    """
    if not str(url).strip():
        raise ValueError("url must not be empty")
    if duration <= 0:
        raise ValueError("duration must be positive")
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if stall_timeout <= 0:
        raise ValueError("stall_timeout must be positive")
    if max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be >= 1")

    target = float(duration)
    chunk_seconds = max(MIN_CHUNK_SECONDS, float(chunk_seconds))
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and not overwrite:
        raise RecorderError(f"Refusing to overwrite existing file: {output_path}")

    ffmpeg = _which("ffmpeg")
    ffprobe = _which("ffprobe")

    work_dir = Path(tempfile.mkdtemp(prefix="lane_record_", dir=str(output_path.parent)))
    chunks: list[Path] = []
    chunk_durations: list[float] = []
    collected = 0.0
    attempts = 0
    consecutive_failures = 0

    log.info("Recording %.1fs from %s (chunk=%.0fs)", target, url, chunk_seconds)

    try:
        while collected < target and attempts < max_attempts:
            attempts += 1
            remaining = target - collected
            chunk_target = max(
                MIN_CHUNK_SECONDS,
                min(chunk_seconds, remaining + min(2.0, remaining * 0.1)),
            )
            chunk_path = work_dir / f"chunk_{attempts:03d}.ts"

            captured, error = _record_chunk(
                url,
                chunk_path,
                chunk_target,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                stall_timeout=stall_timeout,
                reencode_fallback=reencode_fallback,
            )

            if captured < MIN_CHUNK_SECONDS:
                consecutive_failures += 1
                log.warning("Attempt %d captured %.1fs (%s)", attempts, captured, _last_error(error))
                if consecutive_failures >= max_consecutive_failures:
                    log.warning(
                        "Stream stopped producing video after %d attempt(s); keeping %.1fs of %.1fs",
                        attempts,
                        collected,
                        target,
                    )
                    break
                continue

            consecutive_failures = 0
            chunks.append(chunk_path)
            chunk_durations.append(captured)
            collected += captured
            log.info("Chunk %d: %.1fs (%.1fs / %.1fs)", attempts, captured, collected, target)

        if not chunks:
            raise RecorderError(
                f"No video could be captured from {url}. "
                "Check that the stream is live and reachable."
            )

        actual, reencoded = _finalize(
            chunks,
            output_path,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            work_dir=work_dir,
            reencode_fallback=reencode_fallback,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    result = RecordResult(
        output=str(output_path),
        requested_seconds=target,
        actual_seconds=round(actual, 3),
        attempts=attempts,
        chunk_count=len(chunks),
        reencoded=reencoded,
        chunk_durations=[round(value, 3) for value in chunk_durations],
    )
    if result.complete:
        log.info(
            "Wrote %s: %.1fs in %d chunk(s)%s",
            output_path,
            result.actual_seconds,
            result.chunk_count,
            " (re-encoded)" if result.reencoded else "",
        )
    else:
        log.warning(
            "Wrote %s but only %.1fs of %.1fs requested",
            output_path,
            result.actual_seconds,
            target,
        )
    return result
