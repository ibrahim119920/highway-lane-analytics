"""Tests for the Nextcloud WebDAV media uploader (no real network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lane_analytics.media import (
    MediaError,
    NextcloudConfig,
    NextcloudUploader,
    build_media_manifest,
    load_media_manifest,
    media_id_for,
    upload_evidence,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, headers: dict | None = None, text: str = ""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class FakeSession:
    def __init__(self, *, fail_put: int = 0):
        self.calls: list[tuple[str, str]] = []
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.fail_put = fail_put

    def request(self, method, url, data=None, headers=None, auth=None, timeout=None, verify=None):
        self.calls.append((method, url))
        if method == "MKCOL":
            self.dirs.add(url)
            return FakeResponse(201)
        if method == "PUT":
            if self.fail_put:
                self.fail_put -= 1
                return FakeResponse(503, text="busy")
            self.files[url] = data or b""
            return FakeResponse(201)
        return FakeResponse(200)

    def head(self, url, auth=None, timeout=None, verify=None):
        self.calls.append(("HEAD", url))
        if url in self.files:
            return FakeResponse(200, {"Content-Length": str(len(self.files[url]))})
        return FakeResponse(404)


def make_index(tmp_path: Path, *, with_thumbnail: bool = True) -> Path:
    evidence = tmp_path / "evidence"
    clips = evidence / "clips"
    snaps = evidence / "snapshots"
    clips.mkdir(parents=True)
    snaps.mkdir(parents=True)
    (clips / "clip_000060.mp4").write_bytes(b"video-six")
    (clips / "clip_000042.mp4").write_bytes(b"video-forty-two")
    if with_thumbnail:
        (snaps / "snap_000060.jpg").write_bytes(b"jpeg")
    index = {
        "run_id": "run_a",
        "camera_id": "cam_01",
        "generated_at": "2026-11-08T00:00:00Z",
        "events": [
            {
                "event_id": "e1",
                "event_type": "lane_hogging_indication",
                "timestamp": "2026-11-08T06:58:12",
                "clip": "clips/clip_000060.mp4",
                "snapshot": "snapshots/snap_000060.jpg",
                "clip_duration_s": 8.0,
                "clip_start_s": 2.0,
                "clip_end_s": 10.0,
            },
            {
                "event_id": "e2",
                "event_type": "lane_hogging_indication",
                "timestamp": "2026-11-08T06:58:13",
                "clip": "clips/clip_000060.mp4",
                "snapshot": "snapshots/snap_000060.jpg",
                "clip_duration_s": 8.0,
                "clip_start_s": 2.0,
                "clip_end_s": 10.0,
            },
            {
                "event_id": "e3",
                "event_type": "lane_enter",
                "timestamp": "2026-11-08T06:58:10",
                "clip": "clips/clip_000042.mp4",
                "snapshot": "snapshots/snap_000042.jpg",
                "clip_duration_s": 8.0,
                "clip_start_s": 0.0,
                "clip_end_s": 8.0,
            },
        ],
    }
    path = evidence / "index.json"
    path.write_text(json.dumps(index), encoding="utf-8")
    return path


def test_config_strips_whitespace_from_secrets(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", " https://cloud.example/ ")
    monkeypatch.setenv("NEXTCLOUD_USER", " lane-upload\n")
    monkeypatch.setenv("NEXTCLOUD_APP_PASSWORD", "  secret  \n")
    monkeypatch.setenv("NEXTCLOUD_ROOT", " TrafficAnalytics ")

    config = NextcloudConfig.from_env()

    assert config.base_url == "https://cloud.example"
    assert config.username == "lane-upload"
    assert config.app_password == "secret"
    assert config.root == "TrafficAnalytics"


def test_auth_failure_message_points_at_credentials():
    class RejectSession:
        def request(self, *args, **kwargs):
            return FakeResponse(401, text="<s:message>No public access</s:message>")

    uploader = NextcloudUploader(
        NextcloudConfig(username="u", app_password="p", retries=1),
        session=RejectSession(),
    )
    with pytest.raises(MediaError, match="credentials"):
        uploader.ensure_dir("TrafficAnalytics")


def test_build_manifest_filters_and_dedupes_clips(tmp_path: Path):
    index = make_index(tmp_path)
    entries = build_media_manifest(index, root="TrafficAnalytics")

    assert len(entries) == 1  # lane_enter is filtered out
    entry = entries[0]
    assert entry.event_type == "lane_hogging_indication"
    assert entry.event_ids == ["e1", "e2"]
    assert entry.video_path == (
        "TrafficAnalytics/events/cam_01/2026/11/08/run_a/clips/clip_000060.mp4"
    )
    assert entry.thumbnail_path == (
        "TrafficAnalytics/events/cam_01/2026/11/08/run_a/snapshots/snap_000060.jpg"
    )
    assert entry.media_id == media_id_for("run_a", "cam_01", "clips/clip_000060.mp4")


def test_build_manifest_uses_camera_override_and_skips_missing_snapshot(tmp_path: Path):
    index = make_index(tmp_path, with_thumbnail=False)
    entries = build_media_manifest(index, camera_id="cam_99")
    assert entries[0].video_path.startswith("TrafficAnalytics/events/cam_99/")
    assert entries[0].thumbnail_path is None


def test_build_manifest_requires_camera_id(tmp_path: Path):
    index = make_index(tmp_path)
    data = json.loads(index.read_text(encoding="utf-8"))
    data["camera_id"] = ""
    index.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MediaError, match="camera_id"):
        build_media_manifest(index)


def test_media_id_is_stable():
    a = media_id_for("run_a", "cam_01", "clips/clip_000060.mp4")
    b = media_id_for("run_a", "cam_01", "clips/clip_000060.mp4")
    assert a == b
    assert len(a) == 16
    assert media_id_for("run_b", "cam_01", "clips/clip_000060.mp4") != a


def test_dry_run_writes_manifest_without_http(tmp_path: Path):
    index = make_index(tmp_path)
    target = tmp_path / "media.json"
    result = upload_evidence(index, dry_run=True, manifest_path=target, skip_existing=False)

    assert result.uploaded == 0
    assert result.entries[0].status == "PLANNED"
    assert result.by_event_id() == {}
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["entries"][0]["status"] == "PLANNED"


def test_upload_evidence_uploads_clip_and_thumbnail(tmp_path: Path):
    index = make_index(tmp_path)
    session = FakeSession()
    config = NextcloudConfig(username="user", app_password="pass", retries=1)
    target = tmp_path / "media.json"

    result = upload_evidence(
        index,
        config=config,
        session=session,
        manifest_path=target,
        skip_existing=False,
        sleep=lambda _: None,
    )

    assert result.uploaded == 1
    assert result.failed == 0
    assert result.entries[0].status == "COMPLETED"
    assert len(session.files) == 2  # clip + thumbnail
    fields = result.by_event_id()["e1"]
    assert fields["media_status"] == "COMPLETED"
    assert fields["video_path"].endswith("clip_000060.mp4")
    assert json.loads(target.read_text(encoding="utf-8"))["uploaded"] == 1
    # Every PUT is followed by a HEAD size check.
    media_calls = [call for call in session.calls if call[0] in {"PUT", "HEAD"}]
    assert media_calls[0][0] == "PUT"
    assert media_calls[1] == ("HEAD", media_calls[0][1])
    assert media_calls[2][0] == "PUT"
    assert media_calls[3] == ("HEAD", media_calls[2][1])


def test_upload_is_failed_when_head_size_does_not_match(tmp_path: Path):
    index = make_index(tmp_path)
    config = NextcloudConfig(username="user", app_password="pass", retries=1)

    class ShortClipSession(FakeSession):
        def head(self, url, auth=None, timeout=None, verify=None):
            if url.endswith(".mp4"):
                self.calls.append(("HEAD", url))
                return FakeResponse(200, {"Content-Length": "1"})
            return super().head(url, auth=auth, timeout=timeout, verify=verify)

    target = tmp_path / "media.json"
    result = upload_evidence(
        index,
        config=config,
        session=ShortClipSession(),
        skip_existing=False,
        manifest_path=target,
        sleep=lambda _: None,
    )

    assert result.failed == 1
    assert result.entries[0].status == "FAILED"
    assert "verification" in result.entries[0].error
    # A file that was not verified must not be advertised to the pipeline.
    assert result.by_event_id() == {}
    assert json.loads(target.read_text(encoding="utf-8"))["uploaded"] == 0


def test_partial_status_when_thumbnail_verification_fails(tmp_path: Path):
    index = make_index(tmp_path)
    config = NextcloudConfig(username="user", app_password="pass", retries=1)

    class ShortThumbSession(FakeSession):
        def head(self, url, auth=None, timeout=None, verify=None):
            if url.endswith(".jpg"):
                self.calls.append(("HEAD", url))
                return FakeResponse(200, {"Content-Length": "1"})
            return super().head(url, auth=auth, timeout=timeout, verify=verify)

    result = upload_evidence(
        index,
        config=config,
        session=ShortThumbSession(),
        skip_existing=False,
        sleep=lambda _: None,
    )
    assert result.entries[0].status == "PARTIAL"
    assert result.failed == 0
    assert result.by_event_id()["e1"]["media_status"] == "PARTIAL"


def test_clip_without_thumbnail_is_partial(tmp_path: Path):
    index = make_index(tmp_path, with_thumbnail=False)
    result = upload_evidence(
        index,
        config=NextcloudConfig(username="user", app_password="pass", retries=1),
        session=FakeSession(),
        skip_existing=False,
        sleep=lambda _: None,
    )
    assert result.entries[0].status == "PARTIAL"
    assert "thumbnail_path" not in result.by_event_id()["e1"]


def test_upload_retries_transient_put(tmp_path: Path):
    index = make_index(tmp_path)
    session = FakeSession(fail_put=1)
    config = NextcloudConfig(username="user", app_password="pass", retries=2)
    result = upload_evidence(
        index,
        config=config,
        session=session,
        skip_existing=False,
        sleep=lambda _: None,
    )
    assert result.entries[0].status == "COMPLETED"


def test_upload_skips_when_remote_size_matches(tmp_path: Path):
    index = make_index(tmp_path)
    entries = build_media_manifest(index)
    session = FakeSession()
    uploader = NextcloudUploader(
        NextcloudConfig(username="user", app_password="pass"), session=session
    )
    for remote_path, local in (
        (entries[0].video_path, entries[0].local_clip),
        (entries[0].thumbnail_path, entries[0].local_thumbnail),
    ):
        session.files[uploader.dav_url(remote_path)] = Path(local).read_bytes()

    result = upload_evidence(
        index,
        config=NextcloudConfig(username="user", app_password="pass"),
        session=session,
        skip_existing=True,
        sleep=lambda _: None,
    )
    assert result.skipped == 1
    assert result.uploaded == 0


def test_partial_status_when_thumbnail_fails(tmp_path: Path):
    index = make_index(tmp_path)
    config = NextcloudConfig(username="user", app_password="pass", retries=1)

    class NoThumbSession(FakeSession):
        def request(self, method, url, data=None, headers=None, auth=None, timeout=None, verify=None):
            if method == "PUT" and url.endswith(".jpg"):
                return FakeResponse(500, text="thumbnail failed")
            return super().request(method, url, data, headers, auth, timeout, verify)

    result = upload_evidence(
        index,
        config=config,
        session=NoThumbSession(),
        skip_existing=False,
        sleep=lambda _: None,
    )
    assert result.entries[0].status == "PARTIAL"
    assert result.failed == 0
    assert result.by_event_id()["e1"]["media_status"] == "PARTIAL"


def test_missing_credentials_raise(tmp_path: Path):
    index = make_index(tmp_path)
    with pytest.raises(MediaError, match="NEXTCLOUD_USER"):
        upload_evidence(
            index,
            config=NextcloudConfig(username="", app_password=""),
            session=FakeSession(),
            sleep=lambda _: None,
        )


def test_config_from_environment(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://cloud.example/")
    monkeypatch.setenv("NEXTCLOUD_USER", "lane-upload")
    monkeypatch.setenv("NEXTCLOUD_APP_PASSWORD", "secret")
    monkeypatch.setenv("NEXTCLOUD_ROOT", "TrafficAnalytics")
    monkeypatch.setenv("NEXTCLOUD_VERIFY_TLS", "false")
    config = NextcloudConfig.from_env()
    # Trailing slashes are normalised away so DAV URLs never contain "//".
    assert config.base_url == "https://cloud.example"
    assert config.username == "lane-upload"
    assert config.root == "TrafficAnalytics"
    assert config.verify_tls is False
    config.validate()


def test_config_from_env_reads_notebook_secrets(monkeypatch):
    """Kaggle/Colab Secrets are not environment variables."""
    import lane_analytics.media as media

    values = {
        "NEXTCLOUD_URL": "https://cloud.example",
        "NEXTCLOUD_USER": "lane-upload",
        "NEXTCLOUD_APP_PASSWORD": "from-secret",
    }
    monkeypatch.delenv("NEXTCLOUD_URL", raising=False)
    monkeypatch.setattr(media, "_secret", values.get)
    config = media.NextcloudConfig.from_env()
    assert config.username == "lane-upload"
    assert config.app_password == "from-secret"
    config.validate()


def test_load_manifest_maps_duration_to_clip_duration(tmp_path: Path):
    index = make_index(tmp_path)
    target = tmp_path / "media.json"
    upload_evidence(
        index,
        config=NextcloudConfig(username="user", app_password="pass"),
        session=FakeSession(),
        manifest_path=target,
        skip_existing=False,
        sleep=lambda _: None,
    )
    mapping = load_media_manifest(target)
    assert mapping["e1"]["clip_duration_s"] == 8.0
    assert mapping["e1"]["media_status"] == "COMPLETED"


def make_multi_index(tmp_path: Path, *, count: int = 4) -> Path:
    evidence = tmp_path / "evidence"
    clips = evidence / "clips"
    snaps = evidence / "snapshots"
    clips.mkdir(parents=True)
    snaps.mkdir(parents=True)
    events = []
    for index in range(count):
        frame = 60 + index * 15
        clip = f"clips/clip_{frame:06d}.mp4"
        snapshot = f"snapshots/snap_{frame:06d}.jpg"
        (evidence / clip).write_bytes(f"video-{index}".encode())
        (evidence / snapshot).write_bytes(f"image-{index}".encode())
        events.append(
            {
                "event_id": f"e{index}",
                "event_type": "lane_hogging_indication",
                "timestamp": "2026-11-08T06:58:10",
                "clip": clip,
                "snapshot": snapshot,
                "clip_duration_s": 8.0,
                "clip_start_s": 0.0,
                "clip_end_s": 8.0,
            }
        )
    index = {"run_id": "run_multi", "camera_id": "cam_01", "events": events}
    path = evidence / "index.json"
    path.write_text(json.dumps(index), encoding="utf-8")
    return path


def test_ensure_dir_creates_each_level_once():
    session = FakeSession()
    uploader = NextcloudUploader(
        NextcloudConfig(username="user", app_password="pass"), session=session
    )
    clips = "TrafficAnalytics/events/cam_01/2026/11/08/run_a/clips"

    uploader.ensure_dir(clips)
    first = [call for call in session.calls if call[0] == "MKCOL"]
    assert len(first) == len(clips.split("/"))

    uploader.ensure_dir(clips)
    uploader.ensure_dir(clips.rsplit("/", 1)[0] + "/snapshots")
    after = [call for call in session.calls if call[0] == "MKCOL"]
    # Only the new ``snapshots`` level needs a request; the prefix is cached.
    assert len(after) == len(clips.split("/")) + 1


def test_upload_evidence_uploads_multiple_files_in_parallel(tmp_path: Path):
    index = make_multi_index(tmp_path, count=4)
    session = FakeSession()

    result = upload_evidence(
        index,
        config=NextcloudConfig(username="user", app_password="pass", retries=1),
        session=session,
        skip_existing=False,
        workers=2,
        sleep=lambda _: None,
    )

    assert result.uploaded == 4
    assert result.failed == 0
    assert len(result.by_event_id()) == 4
    assert sum(1 for call in session.calls if call[0] == "PUT") == 8  # 4 clips + 4 snaps


def test_upload_workers_read_from_secret(monkeypatch):
    import lane_analytics.media as media

    monkeypatch.setattr(media, "_secret", {"NEXTCLOUD_UPLOAD_WORKERS": "3"}.get)
    assert media._upload_workers() == 3
    assert media._upload_workers(0) == 1
