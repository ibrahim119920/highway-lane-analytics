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
    assert config.base_url == "https://cloud.example/"
    assert config.username == "lane-upload"
    assert config.root == "TrafficAnalytics"
    assert config.verify_tls is False
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
