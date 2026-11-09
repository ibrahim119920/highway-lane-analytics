"""Upload evidence clips to Nextcloud over WebDAV.

The vision pipeline already writes an evidence index for every run:

    output/evidence/<run_id>/index.json
    output/evidence/<run_id>/clips/*.mp4
    output/evidence/<run_id>/snapshots/*.jpg

This module turns the selected entries into a media manifest, uploads the files to
Nextcloud, and returns fields that can be merged into the JSON events before they
are sent to the pipeline. Only metadata travels over the webhook; the video files
stay in Nextcloud.

Typical use from Python::

    from lane_analytics.media import upload_evidence

    result = upload_evidence(
        "output/evidence/<run_id>/index.json",
        camera_id="tj_priok_km58_400b",
    )
    media_by_event = result.by_event_id()

Credentials come from the environment (or Kaggle/Colab Secrets):

    NEXTCLOUD_URL=https://cloud.syaifudin-fam.my.id
    NEXTCLOUD_USER=lane-upload
    NEXTCLOUD_APP_PASSWORD=...
    NEXTCLOUD_ROOT=TrafficAnalytics
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://cloud.syaifudin-fam.my.id"
DEFAULT_ROOT = "TrafficAnalytics"
DEFAULT_EVENT_TYPES = ("lane_hogging_indication",)
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_SIMPLE_UPLOAD_MB = 90.0
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
READY_STATUSES = {"COMPLETED", "PARTIAL", "SKIPPED"}

CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webm": "video/webm",
}


class MediaError(RuntimeError):
    """Raised when media preparation or upload cannot continue."""


def _secret(name: str) -> str | None:
    """Read a value from the environment, Kaggle Secrets, or Colab Secrets.

    Kaggle/Colab Secrets are not exported as environment variables, so reading
    ``os.getenv`` alone would silently skip media uploads inside a notebook.
    """
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


@dataclass
class NextcloudConfig:
    base_url: str = DEFAULT_BASE_URL
    username: str = ""
    app_password: str = ""
    root: str = DEFAULT_ROOT
    timeout: float = DEFAULT_TIMEOUT
    retries: int = 4
    max_simple_upload_mb: float = DEFAULT_MAX_SIMPLE_UPLOAD_MB
    verify_tls: bool = True

    def __post_init__(self) -> None:
        # Kaggle/Colab secrets are often pasted with a trailing newline or space.
        # Basic auth compares the exact bytes, so a stray character turns into a
        # misleading 401 ("AppAPIAuth has not passed").
        self.base_url = str(self.base_url).strip().rstrip("/")
        self.username = str(self.username).strip()
        self.app_password = str(self.app_password).strip()
        self.root = str(self.root).strip().strip("/")

    @classmethod
    def from_env(cls, **overrides: Any) -> "NextcloudConfig":
        values: dict[str, Any] = {
            "base_url": _secret("NEXTCLOUD_URL") or DEFAULT_BASE_URL,
            "username": _secret("NEXTCLOUD_USER") or "",
            "app_password": _secret("NEXTCLOUD_APP_PASSWORD") or "",
            "root": _secret("NEXTCLOUD_ROOT") or DEFAULT_ROOT,
            "timeout": float(_secret("NEXTCLOUD_TIMEOUT") or DEFAULT_TIMEOUT),
            "retries": int(_secret("NEXTCLOUD_RETRIES") or "4"),
            "max_simple_upload_mb": float(
                _secret("NEXTCLOUD_MAX_SIMPLE_UPLOAD_MB") or DEFAULT_MAX_SIMPLE_UPLOAD_MB
            ),
            "verify_tls": (_secret("NEXTCLOUD_VERIFY_TLS") or "true").lower()
            not in {"0", "false", "no"},
        }
        values.update(overrides)
        return cls(**values)

    def validate(self) -> None:
        if not str(self.base_url).strip():
            raise MediaError("NEXTCLOUD_URL is empty")
        if not str(self.username).strip():
            raise MediaError("NEXTCLOUD_USER is empty")
        if not str(self.app_password).strip():
            raise MediaError("NEXTCLOUD_APP_PASSWORD is empty")
        if not str(self.root).strip():
            raise MediaError("NEXTCLOUD_ROOT is empty")


@dataclass
class MediaEntry:
    """One clip (and optional thumbnail) and the events that reference it."""

    media_id: str
    event_ids: list[str]
    event_type: str
    local_clip: str
    local_thumbnail: str | None
    video_path: str
    thumbnail_path: str | None
    file_size: int
    duration_s: float
    clip_start_s: float
    clip_end_s: float
    status: str = "PENDING"
    error: str = ""

    def as_event_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "media_id": self.media_id,
            "video_path": self.video_path,
            "media_status": self.status,
            "file_size": self.file_size,
            "clip_duration_s": self.duration_s,
            "clip_start_s": self.clip_start_s,
            "clip_end_s": self.clip_end_s,
        }
        if self.thumbnail_path:
            fields["thumbnail_path"] = self.thumbnail_path
        return fields

    def as_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "event_ids": list(self.event_ids),
            "event_type": self.event_type,
            "video_path": self.video_path,
            "thumbnail_path": self.thumbnail_path,
            "file_size": self.file_size,
            "duration_s": self.duration_s,
            "clip_start_s": self.clip_start_s,
            "clip_end_s": self.clip_end_s,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class MediaUploadResult:
    run_id: str
    camera_id: str
    root: str
    entries: list[MediaEntry] = field(default_factory=list)
    manifest_path: str = ""
    dry_run: bool = False

    @property
    def uploaded(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "COMPLETED")

    @property
    def skipped(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "SKIPPED")

    @property
    def failed(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "FAILED")

    def by_event_id(self) -> dict[str, dict[str, Any]]:
        mapping: dict[str, dict[str, Any]] = {}
        for entry in self.entries:
            if entry.status not in READY_STATUSES:
                continue
            fields = entry.as_event_fields()
            for event_id in entry.event_ids:
                if event_id:
                    mapping[event_id] = fields
        return mapping

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "camera_id": self.camera_id,
            "root": self.root,
            "dry_run": self.dry_run,
            "uploaded": self.uploaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "manifest_path": self.manifest_path,
            "entries": [entry.as_dict() for entry in self.entries],
        }


# --------------------------------------------------------------- manifest
def media_id_for(run_id: str, camera_id: str, relative_clip_path: str) -> str:
    """Stable, URL-safe id for one clip."""
    raw = f"{run_id}|{camera_id}|{relative_clip_path}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _event_date(index: dict[str, Any]) -> date:
    for event in index.get("events") or []:
        value = event.get("timestamp")
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError:
            continue
    generated = index.get("generated_at")
    if generated:
        try:
            return datetime.fromisoformat(str(generated).replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return date.today()


def build_media_manifest(
    index_path: str | Path,
    *,
    camera_id: str | None = None,
    root: str = DEFAULT_ROOT,
    event_types: Sequence[str] = DEFAULT_EVENT_TYPES,
) -> list[MediaEntry]:
    """Read ``evidence/index.json`` and return one entry per selected clip."""
    path = Path(index_path)
    if not path.exists():
        raise MediaError(f"Evidence index not found: {path}")
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MediaError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(index, dict):
        raise MediaError(f"{path} must contain a JSON object")

    run_id = str(index.get("run_id") or "").strip()
    if not run_id:
        raise MediaError(f"{path} has no run_id")
    camera = str(camera_id or index.get("camera_id") or "").strip()
    if not camera:
        raise MediaError(
            f"{path} has no camera_id; pass camera_id explicitly so the Nextcloud "
            "path cannot end up under an 'unknown' folder"
        )
    allowed = {str(name) for name in event_types} if event_types else None
    index_dir = path.parent
    day = _event_date(index)
    run_dir = (
        f"{str(root).strip('/')}/events/{camera}/"
        f"{day:%Y/%m/%d}/{run_id}"
    )

    grouped: dict[str, dict[str, Any]] = {}
    for event in index.get("events") or []:
        event_type = str(event.get("event_type") or "")
        if allowed is not None and event_type not in allowed:
            continue
        clip_rel = str(event.get("clip") or "").strip().replace("\\", "/")
        if not clip_rel:
            continue
        event_id = str(event.get("event_id") or "").strip()
        group = grouped.setdefault(
            clip_rel,
            {
                "event_ids": [],
                "event_type": event_type,
                "snapshot": event.get("snapshot"),
                "duration": float(event.get("clip_duration_s") or 0.0),
                "start": float(event.get("clip_start_s") or 0.0),
                "end": float(event.get("clip_end_s") or 0.0),
            },
        )
        if event_id and event_id not in group["event_ids"]:
            group["event_ids"].append(event_id)
        if not group.get("snapshot") and event.get("snapshot"):
            group["snapshot"] = event.get("snapshot")

    entries: list[MediaEntry] = []
    for clip_rel, group in grouped.items():
        local_clip = index_dir / clip_rel
        if not local_clip.exists():
            log.warning("Evidence clip missing on disk: %s", local_clip)
            continue
        snapshot_rel = str(group.get("snapshot") or "").strip().replace("\\", "/")
        local_thumb = index_dir / snapshot_rel if snapshot_rel else None
        if local_thumb is not None and not local_thumb.exists():
            local_thumb = None
        entries.append(
            MediaEntry(
                media_id=media_id_for(run_id, camera, clip_rel),
                event_ids=list(group["event_ids"]),
                event_type=str(group.get("event_type") or ""),
                local_clip=str(local_clip),
                local_thumbnail=str(local_thumb) if local_thumb else None,
                video_path=posixpath.join(run_dir, clip_rel),
                thumbnail_path=(
                    posixpath.join(run_dir, snapshot_rel) if local_thumb else None
                ),
                file_size=local_clip.stat().st_size,
                duration_s=float(group.get("duration") or 0.0),
                clip_start_s=float(group.get("start") or 0.0),
                clip_end_s=float(group.get("end") or 0.0),
            )
        )
    entries.sort(key=lambda entry: entry.video_path)
    return entries


def load_media_manifest(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load a saved manifest and return ``event_id -> event media fields``."""
    source = Path(path)
    if not source.exists():
        raise MediaError(f"Media manifest not found: {source}")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MediaError(f"{source} is not valid JSON: {exc}") from exc
    mapping: dict[str, dict[str, Any]] = {}
    for entry in data.get("entries") or []:
        status = str(entry.get("status") or "")
        if status not in READY_STATUSES:
            continue
        fields: dict[str, Any] = {
            key: entry[key]
            for key in (
                "media_id",
                "video_path",
                "thumbnail_path",
                "file_size",
                "clip_start_s",
                "clip_end_s",
            )
            if entry.get(key) is not None
        }
        if entry.get("duration_s") is not None:
            fields["clip_duration_s"] = entry["duration_s"]
        fields["media_status"] = status
        for event_id in entry.get("event_ids") or []:
            if event_id:
                mapping[str(event_id)] = fields
    return mapping


def save_media_manifest(result: MediaUploadResult, path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return str(target)


# --------------------------------------------------------------- uploader
class NextcloudUploader:
    """Minimal WebDAV uploader for Nextcloud.

    Clips produced by this project are short (about 1-5 MB), so a single PUT is
    used. Files larger than ``max_simple_upload_mb`` are rejected with a clear
    message instead of silently hitting a PHP/Cloudflare body limit.
    """

    def __init__(
        self,
        config: NextcloudConfig,
        *,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ) -> None:
        config.validate()
        self.config = config
        self.session = session or requests.Session()
        self.sleep = sleep

    # ---------------------------------------------------------------- helpers
    def _base(self) -> str:
        return self.config.base_url.strip().rstrip("/")

    def _user(self) -> str:
        return quote(self.config.username.strip(), safe="")

    def _path(self, remote_path: str) -> str:
        parts = [
            quote(part, safe="")
            for part in remote_path.replace("\\", "/").strip("/").split("/")
            if part
        ]
        return "/".join(parts)

    def dav_url(self, remote_path: str) -> str:
        return f"{self._base()}/remote.php/dav/files/{self._user()}/{self._path(remote_path)}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 204, 207),
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, max(1, self.config.retries) + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    auth=(self.config.username, self.config.app_password),
                    timeout=self.config.timeout,
                    verify=self.config.verify_tls,
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code in expected:
                    return response
                if (
                    response.status_code in RETRY_STATUS
                    and attempt < self.config.retries
                ):
                    last_error = MediaError(
                        f"{method} {url} -> {response.status_code}"
                    )
                else:
                    if response.status_code in (401, 403):
                        raise MediaError(
                            f"{method} {url} -> {response.status_code}: Nextcloud "
                            "rejected the credentials. Check NEXTCLOUD_USER and "
                            "NEXTCLOUD_APP_PASSWORD (the app password may have been "
                            "rotated); whitespace is already stripped. "
                            f"{_response_text(response)}"
                        )
                    raise MediaError(
                        f"{method} {url} -> {response.status_code}: "
                        f"{_response_text(response)}"
                    )
            if attempt < self.config.retries:
                delay = min(2 ** (attempt - 1), 16) + 0.1 * attempt
                log.warning(
                    "Nextcloud retry %d/%d in %.1fs: %s",
                    attempt,
                    self.config.retries,
                    delay,
                    last_error,
                )
                self.sleep(delay)
        raise MediaError(f"{method} {url} failed after retries: {last_error}")

    def ensure_dir(self, remote_dir: str) -> None:
        parts = [
            part
            for part in remote_dir.replace("\\", "/").strip("/").split("/")
            if part
        ]
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            self._request("MKCOL", self.dav_url(current), expected=(201, 405))

    def remote_size(self, remote_path: str) -> int | None:
        try:
            response = self.session.head(
                self.dav_url(remote_path),
                auth=(self.config.username, self.config.app_password),
                timeout=self.config.timeout,
                verify=self.config.verify_tls,
            )
        except requests.RequestException:
            return None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            return None
        value = response.headers.get("Content-Length")
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    def verify_upload(
        self,
        remote_path: str,
        expected_size: int,
        *,
        attempts: int | None = None,
    ) -> bool:
        """Confirm the stored file has the expected size.

        A PUT that returns 2xx is not proof that Nextcloud stored the whole body
        (a truncated proxy/PHP upload can still answer 201). ``COMPLETED`` is
        only reported after this HEAD check matches.
        """
        total = max(1, attempts if attempts is not None else self.config.retries)
        for attempt in range(1, total + 1):
            if self.remote_size(remote_path) == expected_size:
                return True
            if attempt < total:
                self.sleep(min(2 ** (attempt - 1), 5) * 0.5)
        return False

    def upload_file(self, local_path: str | Path, remote_path: str) -> bool:
        source = Path(local_path)
        if not source.exists():
            raise MediaError(f"Local file not found: {source}")
        size = source.stat().st_size
        if size > self.config.max_simple_upload_mb * 1024 * 1024:
            raise MediaError(
                f"{source.name} is {size / 1e6:.1f} MB; simple WebDAV PUT is limited "
                f"to {self.config.max_simple_upload_mb:.0f} MB. Shorten the clip or "
                "add chunked upload support."
            )
        content_type = CONTENT_TYPES.get(source.suffix.lower(), "application/octet-stream")
        self.ensure_dir(posixpath.dirname(remote_path))
        self._request(
            "PUT",
            self.dav_url(remote_path),
            data=source.read_bytes(),
            headers={"Content-Type": content_type, "Content-Length": str(size)},
            expected=(200, 201, 204),
        )
        return True

    def upload_entry(self, entry: MediaEntry, *, skip_existing: bool = True) -> MediaEntry:
        try:
            existing = self.remote_size(entry.video_path) if skip_existing else None
            if existing is not None and existing == entry.file_size:
                entry.status = "SKIPPED"
            else:
                self.upload_file(entry.local_clip, entry.video_path)
                if not self.verify_upload(entry.video_path, entry.file_size):
                    raise MediaError(
                        "clip size verification failed after PUT "
                        f"({entry.video_path})"
                    )
                entry.status = "COMPLETED"
        except Exception as exc:
            entry.status = "FAILED"
            entry.error = f"{type(exc).__name__}: {exc}"
            return entry

        if entry.local_thumbnail and entry.thumbnail_path:
            try:
                thumbnail_size = Path(entry.local_thumbnail).stat().st_size
                self.upload_file(entry.local_thumbnail, entry.thumbnail_path)
                if not self.verify_upload(entry.thumbnail_path, thumbnail_size):
                    raise MediaError(
                        "thumbnail size verification failed after PUT "
                        f"({entry.thumbnail_path})"
                    )
            except Exception as exc:
                entry.status = "PARTIAL"
                entry.error = f"thumbnail: {type(exc).__name__}: {exc}"
        elif entry.status == "COMPLETED":
            # The clip is usable, but the contract reserves COMPLETED for
            # clip + thumbnail pairs.
            entry.status = "PARTIAL"
            entry.error = "thumbnail: not available"
        return entry


def _response_text(response: requests.Response, limit: int = 300) -> str:
    try:
        return response.text[:limit].replace("\n", " ")
    except Exception:  # pragma: no cover - defensive
        return ""


def upload_evidence(
    index_path: str | Path,
    *,
    config: NextcloudConfig | None = None,
    camera_id: str | None = None,
    root: str = DEFAULT_ROOT,
    event_types: Sequence[str] = DEFAULT_EVENT_TYPES,
    dry_run: bool = False,
    skip_existing: bool = True,
    manifest_path: str | Path | None = None,
    session: requests.Session | None = None,
    sleep=time.sleep,
) -> MediaUploadResult:
    """Build, upload (unless dry-run), and optionally save the media manifest."""
    entries = build_media_manifest(
        index_path,
        camera_id=camera_id,
        root=root,
        event_types=event_types,
    )
    index = json.loads(Path(index_path).read_text(encoding="utf-8"))
    result = MediaUploadResult(
        run_id=str(index.get("run_id") or ""),
        camera_id=str(camera_id or index.get("camera_id") or "unknown"),
        root=str(root).strip("/"),
        entries=entries,
        dry_run=dry_run,
    )

    if dry_run:
        for entry in entries:
            entry.status = "PLANNED"
        if manifest_path:
            result.manifest_path = save_media_manifest(result, manifest_path)
        return result

    uploader = NextcloudUploader(
        config or NextcloudConfig.from_env(),
        session=session,
        sleep=sleep,
    )
    dirs = sorted({posixpath.dirname(entry.video_path) for entry in entries})
    dirs += sorted(
        {
            posixpath.dirname(entry.thumbnail_path)
            for entry in entries
            if entry.thumbnail_path
        }
    )
    for remote_dir in dict.fromkeys(dirs):
        uploader.ensure_dir(remote_dir)
    for entry in entries:
        uploader.upload_entry(entry, skip_existing=skip_existing)

    if manifest_path:
        result.manifest_path = save_media_manifest(result, manifest_path)
    return result
