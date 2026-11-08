"""Tests for the Colab activation helpers (no downloads, no video decoding)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from lane_analytics.colab import (
    ActivationError,
    _drive_file_id,
    _secret,
    activate,
    discover_videos,
    extract_zip,
    infer_timestamp_from_name,
    plan_base_timestamps,
    unique_run_ids,
)


def test_secret_prefers_environment(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://example.test/hook")
    assert _secret("WEBHOOK_URL") == "https://example.test/hook"


def test_infer_timestamp_from_compact_name():
    assert infer_timestamp_from_name("km58_20261107_161545.mp4") == "2026-11-07T16:15:45"


def test_infer_timestamp_from_iso_like_name():
    assert infer_timestamp_from_name("clip-2026-11-07T23-25-41.mp4") == "2026-11-07T23:25:41"


def test_infer_timestamp_returns_none_without_a_date():
    assert infer_timestamp_from_name("clip_001.mp4") is None


def test_unique_run_ids_are_filename_safe_and_disambiguated():
    videos = [Path("a b.mp4"), Path("a_b.mp4")]
    ids = unique_run_ids(videos)
    assert ids[Path("a b.mp4")] == "a_b"
    assert ids[Path("a_b.mp4")] == "a_b_02"


def test_plan_base_timestamps_advances_by_duration():
    videos = [Path("01.mp4"), Path("02.mp4"), Path("03.mp4")]
    plan = plan_base_timestamps(
        videos,
        base_timestamp="2026-11-07T23:00:00",
        duration_fn=lambda _: 300.0,
    )
    assert plan[videos[0]] == "2026-11-07T23:00:00"
    assert plan[videos[1]] == "2026-11-07T23:05:00"
    assert plan[videos[2]] == "2026-11-07T23:10:00"


def test_plan_base_timestamps_prefers_explicit_mapping():
    videos = [Path("01.mp4"), Path("02.mp4")]
    plan = plan_base_timestamps(
        videos,
        base_timestamp="2026-11-07T23:00:00",
        base_timestamps={"01": "2026-11-07T10:00:00", "02.mp4": "2026-11-07T10:05:00"},
        duration_fn=lambda _: 300.0,
    )
    assert plan[videos[0]] == "2026-11-07T10:00:00"
    assert plan[videos[1]] == "2026-11-07T10:05:00"


def test_plan_base_timestamps_falls_back_to_filename(tmp_path: Path):
    video = tmp_path / "cam_20261107_161545.mp4"
    video.write_bytes(b"")
    plan = plan_base_timestamps([video])
    assert plan[video] == "2026-11-07T16:15:45"


def test_discover_videos_filters_and_sorts(tmp_path: Path):
    (tmp_path / "b.mp4").write_bytes(b"")
    (tmp_path / "a.mp4").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "a_annotated.mp4").write_bytes(b"")
    found = discover_videos(tmp_path)
    assert [path.name for path in found] == ["a.mp4", "b.mp4"]


def test_extract_zip_writes_files(tmp_path: Path):
    archive = tmp_path / "clips.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("nested/clip.mp4", b"video")
        bundle.writestr("__MACOSX/._clip.mp4", b"junk")
    target = extract_zip(archive, tmp_path / "out")
    assert (target / "nested" / "clip.mp4").read_bytes() == b"video"
    assert not (target / "__MACOSX").exists()


def test_extract_zip_rejects_path_traversal(tmp_path: Path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../evil.mp4", b"x")
    with pytest.raises(ActivationError, match="Unsafe path"):
        extract_zip(archive, tmp_path / "out")


def test_drive_file_id_from_common_urls():
    assert _drive_file_id("https://drive.google.com/file/d/ABC123/view?usp=sharing") == "ABC123"
    assert _drive_file_id("https://drive.google.com/uc?id=XYZ789") == "XYZ789"
    assert _drive_file_id("a" * 33) == "a" * 33
    assert _drive_file_id("https://example.com/video.mp4") is None


def test_activate_rejects_unknown_source():
    with pytest.raises(ActivationError, match="source"):
        activate(source="ftp")


def test_activate_requires_drive_url(monkeypatch):
    monkeypatch.delenv("DRIVE_ZIP_URL", raising=False)
    with pytest.raises(ActivationError, match="drive_url"):
        activate(source="drive_zip")

def test_activate_uses_production_pipeline_and_secret(tmp_path: Path, monkeypatch):
    archive = tmp_path / "videos.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("clip.mp4", "dummy")

    captured: dict = {}

    monkeypatch.setattr(
        "lane_analytics.colab._secret",
        lambda name: {"PIPELINE_WEBHOOK_TOKEN": "secret-token"}.get(name),
    )

    def fake_process_one(video, **kwargs):
        return SimpleNamespace(
            total_vehicles=1,
            total_events=1,
            hogging_events=0,
            evidence_events=0,
            evidence_snapshots=0,
            evidence_clips=0,
            evidence_dir=None,
            events_path=str(tmp_path / "events.jsonl"),
            tracks_path=str(tmp_path / "tracks.jsonl"),
            reports_dir=None,
        )

    def fake_replay(path, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(as_dict=lambda: {})

    monkeypatch.setattr("lane_analytics.colab._process_one", fake_process_one)
    monkeypatch.setattr("lane_analytics.colab.replay_run", fake_replay)

    activate(
        source="drive_zip",
        zip_path=archive,
        video_dir=tmp_path / "raw",
        replay=True,
    )

    assert captured["url"] == "https://lane-api.syaifudin-fam.my.id"
    assert captured["token"] == "secret-token"
    assert captured["source"] == "kaggle-replay"


def test_activate_uploads_media_and_merges_manifest(tmp_path: Path, monkeypatch):
    archive = tmp_path / "videos.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("clip.mp4", "dummy")

    index = tmp_path / "index.json"
    index.write_text("{}", encoding="utf-8")
    captured: dict = {}

    def fake_process_one(video, **kwargs):
        return SimpleNamespace(
            total_vehicles=1,
            total_events=1,
            hogging_events=1,
            evidence_events=1,
            evidence_snapshots=1,
            evidence_clips=1,
            evidence_dir=str(tmp_path / "evidence"),
            evidence_index=str(index),
            camera_id="cam_01",
            events_path=str(tmp_path / "events.jsonl"),
            tracks_path=str(tmp_path / "tracks.jsonl"),
            reports_dir=str(tmp_path / "reports"),
        )

    class FakeMediaResult:
        def by_event_id(self):
            return {"e1": {"media_id": "m1", "video_path": "clips/a.mp4"}}

        def as_dict(self):
            return {"uploaded": 1, "failed": 0}

    def fake_upload(*args, **kwargs):
        captured["upload"] = kwargs
        return FakeMediaResult()

    def fake_replay(*args, **kwargs):
        captured["replay"] = kwargs
        return SimpleNamespace(as_dict=lambda: {})

    monkeypatch.setattr("lane_analytics.colab._process_one", fake_process_one)
    monkeypatch.setattr("lane_analytics.media.upload_evidence", fake_upload)
    monkeypatch.setattr("lane_analytics.colab.replay_run", fake_replay)

    result = activate(
        source="drive_zip",
        zip_path=archive,
        video_dir=tmp_path / "raw",
        replay=True,
        webhook_url="https://lane-api.example",
        webhook_token="token",
        upload_media=True,
        media_root="TrafficAnalytics",
    )

    assert result.processed == 1
    assert captured["upload"]["camera_id"] == "cam_01"
    assert captured["replay"]["media_manifest"] == {
        "e1": {"media_id": "m1", "video_path": "clips/a.mp4"}
    }
    assert result.videos[0]["media"]["uploaded"] == 1


def test_activate_accepts_session_and_run_id(tmp_path: Path, monkeypatch):
    archive = tmp_path / "videos.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("clip.mp4", "dummy")

    captured: dict = {}

    def fake_process_one(video, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            total_vehicles=3,
            total_events=5,
            hogging_events=1,
            evidence_events=0,
            evidence_snapshots=0,
            evidence_clips=0,
            evidence_dir=None,
            events_path=str(tmp_path / "events.jsonl"),
            tracks_path=str(tmp_path / "tracks.jsonl"),
            reports_dir=None,
        )

    monkeypatch.setattr("lane_analytics.colab._process_one", fake_process_one)
    result = activate(
        source="drive_zip",
        zip_path=archive,
        video_dir=tmp_path / "raw",
        replay=False,
        session_id="sess_01",
        window_index=2,
        window_count=5,
        run_id="sess_01_w0002",
    )

    assert result.processed == 1
    item = result.videos[0]
    assert item["run_id"] == "sess_01_w0002"
    assert item["session_id"] == "sess_01"
    assert item["window_index"] == 2
    assert item["window_count"] == 5
    assert captured["run_id"] == "sess_01_w0002"
    assert captured["session_id"] == "sess_01"
    assert captured["window_index"] == 2
    assert captured["window_count"] == 5
