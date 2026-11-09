"""Download live windows recorded by the pipeline host.

Kaggle reaches the Hong Kong CCTV origin at about 70 kbps while the stream needs
~600 kbps, so a notebook cannot record it reliably. ``activate(source=
"server_capture")`` asks the pipeline host (which reaches the stream at roughly
1.4 Mbps) to record one window and then downloads the finished MP4 through the
Cloudflare tunnel.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .utils import ensure_dir

DEFAULT_SECONDS = 30.0
DEFAULT_TIMEOUT = 420.0
DEFAULT_STREAM_URL = (
    "https://pub2.hk-opt.com/LiveApp/streams/501264524429471982550467.m3u8"
)


class CaptureError(RuntimeError):
    """Raised when a remote capture cannot be recorded or downloaded."""


def default_capture_base() -> str:
    """Base URL of the capture endpoint (``.../capture`` or the host root)."""
    from .replay import DEFAULT_BASE_URL

    return (
        os.getenv("CAPTURE_BASE_URL")
        or os.getenv("PIPELINE_BASE_URL")
        or DEFAULT_BASE_URL
    )


@dataclass
class CaptureDownload:
    capture_id: str
    path: Path
    started_at: str
    duration_s: float
    size_bytes: int
    backlog_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "path": str(self.path),
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "size_bytes": self.size_bytes,
            "backlog_s": self.backlog_s,
        }


def _endpoint(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise CaptureError("capture base URL is empty")
    return base if base.endswith("/capture") else f"{base}/capture"


def _error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return str(response.text or "").strip()[:200]
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload)[:200]
    return str(payload)[:200]


def fetch_capture(
    base_url: str,
    token: str,
    *,
    seconds: float = DEFAULT_SECONDS,
    url: str | None = None,
    destination: str | Path = "data/raw/remote",
    timeout: float = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
    sleep=time.sleep,
) -> CaptureDownload:
    """Ask the pipeline host to record ``seconds`` and download the MP4.

    The POST blocks for roughly the window length. A 409 response means another
    capture is already running; retry later instead of starting a second one.
    """
    if not token:
        raise CaptureError(
            "a pipeline token is required to request a capture "
            "(PIPELINE_WEBHOOK_TOKEN)"
        )
    endpoint = _endpoint(base_url)
    client = session or requests
    payload: dict[str, Any] = {"seconds": float(seconds)}
    if url:
        payload["url"] = str(url)

    try:
        response = client.post(
            endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=float(timeout),
        )
    except requests.RequestException as exc:
        raise CaptureError(f"capture request failed: {exc}") from exc

    if response.status_code == 409:
        raise CaptureError(
            "the pipeline host is already recording another capture; retry in a "
            "few seconds"
        )
    if response.status_code in (401, 403):
        raise CaptureError("capture request was rejected (check the pipeline token)")
    if response.status_code >= 400:
        raise CaptureError(
            f"capture failed ({response.status_code}): {_error_detail(response)}"
        )
    try:
        meta = response.json()
    except ValueError as exc:
        raise CaptureError("capture response was not JSON") from exc

    download_url = str(meta.get("download_url") or "")
    capture_id = str(meta.get("capture_id") or "")
    if not download_url or not capture_id:
        raise CaptureError("capture response is missing download_url/capture_id")

    folder = ensure_dir(destination)
    target = folder / f"{capture_id}.mp4"
    partial = folder / f"{capture_id}.part.mp4"
    expected = int(meta.get("size_bytes") or 0)
    last_error = ""
    for attempt in range(1, 3):
        try:
            with client.get(download_url, stream=True, timeout=float(timeout)) as stream:
                if stream.status_code != 200:
                    last_error = f"HTTP {stream.status_code}"
                    raise CaptureError(last_error)
                with partial.open("wb") as handle:
                    for chunk in stream.iter_content(64 * 1024):
                        if chunk:
                            handle.write(chunk)
            size = partial.stat().st_size
            if expected and size != expected:
                last_error = f"size mismatch ({size} != {expected})"
                raise CaptureError(last_error)
            partial.replace(target)
            break
        except (requests.RequestException, CaptureError) as exc:
            last_error = str(exc)
            if partial.exists():
                partial.unlink()
            if attempt < 2:
                sleep(2.0)
    else:
        raise CaptureError(f"could not download the capture: {last_error}")

    duration = float(meta.get("duration_s") or 0.0)
    if duration < 1.0:
        target.unlink(missing_ok=True)
        raise CaptureError(f"capture is too short ({duration:.2f}s)")

    return CaptureDownload(
        capture_id=capture_id,
        path=target,
        started_at=str(meta.get("started_at") or ""),
        duration_s=duration,
        size_bytes=target.stat().st_size,
        backlog_s=float(meta.get("backlog_s") or 0.0),
    )
