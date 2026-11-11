"""Tests for the Colab activation helpers (no downloads, no video decoding)."""

from __future__ import annotations

import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from lane_analytics.colab import (
    ActivationError,
    PostQueue,
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
            input_video=str(tmp_path / "clip.mp4"),
            start_frame=0,
            source_fps=18.0,
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

    def fake_evidence(**kwargs):
        captured["evidence"] = kwargs
        return SimpleNamespace(
            index_path=str(index),
            directory=str(tmp_path / "evidence"),
            event_count=1,
            snapshot_count=1,
            clip_count=1,
        )

    monkeypatch.setattr("lane_analytics.colab._process_one", fake_process_one)
    monkeypatch.setattr("lane_analytics.media.upload_evidence", fake_upload)
    monkeypatch.setattr("lane_analytics.evidence.generate_evidence", fake_evidence)
    monkeypatch.setattr("lane_analytics.colab.replay_run", fake_replay)
    monkeypatch.setattr(
        "lane_analytics.colab.post_media_patch", lambda *a, **k: {"sent": 1}
    )

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
    # Events are replayed without media; the manifest arrives via the patch.
    assert captured["replay"]["media_manifest"] is None
    assert result.videos[0]["media"]["uploaded"] == 1
    assert result.videos[0]["media_patch"] == {"sent": 1}
    assert captured["evidence"]["annotate"] is False


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


class FakeMediaResult:
    def by_event_id(self):
        return {"e1": {"media_id": "m1", "video_path": "clips/a.mp4"}}

    def as_dict(self):
        return {"uploaded": 1, "failed": 0}


def make_batch(tmp_path: Path) -> SimpleNamespace:
    index = tmp_path / "index.json"
    index.write_text("{}", encoding="utf-8")
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
        input_video=str(tmp_path / "clip.mp4"),
        start_frame=0,
        source_fps=18.0,
        events_path=str(tmp_path / "events.jsonl"),
        tracks_path=str(tmp_path / "tracks.jsonl"),
        reports_dir=str(tmp_path / "reports"),
    )


def test_post_queue_replays_before_media_finishes():
    sent = threading.Event()
    order = []

    def replay_first():
        order.append("replay-0")
        sent.set()

    def replay_second():
        order.append("replay-1")

    def slow_media():
        sent.wait(5)
        order.append("media-0")
        raise RuntimeError("nextcloud down")

    items = [{"window_index": 0}, {"window_index": 1}]
    queue = PostQueue(upload_workers=1)
    queue.submit(items[0], run_id="w0", replay=replay_first, media=slow_media)
    queue.submit(items[1], run_id="w1", replay=replay_second)
    jobs = queue.drain(timeout=10)

    # Replay never waits for evidence/upload and keeps submission order.
    assert order[:2] == ["replay-0", "replay-1"]
    assert jobs[0].item["media_error"].startswith("RuntimeError")
    assert jobs[0].item["status"] == "ok"
    assert all(job.done.is_set() for job in jobs)


def test_post_queue_marks_replay_failure_on_item():
    def fail():
        raise RuntimeError("webhook down")

    item = {"window_index": 0}
    queue = PostQueue(upload_workers=1)
    queue.submit(item, run_id="w0", replay=fail)
    queue.drain(timeout=10)

    assert item["status"] == "error"
    assert "webhook down" in item["error"]


def test_activate_processes_a_prefetched_file(tmp_path: Path, monkeypatch):
    video = tmp_path / "cap_0001.mp4"
    video.write_bytes(b"fake-video")
    captured: dict = {}

    def fake_process_one(video_path, **kwargs):
        captured["video"] = Path(video_path)
        captured.update(kwargs)
        return make_batch(tmp_path)

    monkeypatch.setattr("lane_analytics.colab._process_one", fake_process_one)

    result = activate(
        source="file",
        video_path=video,
        base_timestamp="2026-11-10T19:00:00",
        replay=False,
        video_dir=tmp_path / "raw",
    )

    assert result.processed == 1
    assert captured["video"] == video
    assert captured["base_timestamp"] == "2026-11-10T19:00:00"
    assert result.videos[0]["run_id"] == "cap_0001"


def test_activate_file_source_requires_existing_video(tmp_path: Path):
    with pytest.raises(ActivationError, match="video_path"):
        activate(source="file", video_path=tmp_path / "missing.mp4", replay=False)


def test_activate_post_queue_keeps_per_video_context(tmp_path: Path, monkeypatch):
    archive = tmp_path / "videos.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("a.mp4", "dummy-a")
        handle.writestr("b.mp4", "dummy-b")

    seen: list[tuple[str, str]] = []

    def fake_process_one(video_path, **kwargs):
        stem = Path(video_path).stem
        batch = make_batch(tmp_path)
        batch.events_path = str(tmp_path / f"{stem}.jsonl")
        batch.tracks_path = str(tmp_path / f"{stem}_tracks.jsonl")
        return batch

    def fake_replay(path, **kwargs):
        seen.append((kwargs["run_id"], path))
        return SimpleNamespace(as_dict=lambda: {})

    monkeypatch.setattr("lane_analytics.colab._process_one", fake_process_one)
    monkeypatch.setattr("lane_analytics.colab.replay_run", fake_replay)

    queue = PostQueue(upload_workers=2)
    activate(
        source="drive_zip",
        zip_path=archive,
        video_dir=tmp_path / "raw",
        replay=True,
        webhook_url="https://lane-api.example",
        webhook_token="token",
        post_queue=queue,
    )
    queue.drain(timeout=10)

    # Each queued replay must use its own video's batch (no late binding).
    assert seen == [
        ("a", str(tmp_path / "a.jsonl")),
        ("b", str(tmp_path / "b.jsonl")),
    ]


def test_activate_post_queue_defers_upload_and_replay(tmp_path: Path, monkeypatch):
    archive = tmp_path / "videos.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("clip.mp4", "dummy")

    release = threading.Event()
    replay_calls: list = []
    patch_calls: list = []

    def fake_process_one(video_path, **kwargs):
        return make_batch(tmp_path)

    def fake_upload(*args, **kwargs):
        release.wait(5)
        return FakeMediaResult()

    def fake_evidence(**kwargs):
        return SimpleNamespace(
            index_path=str(tmp_path / "index.json"),
            directory=str(tmp_path / "evidence"),
            event_count=1,
            snapshot_count=1,
            clip_count=1,
        )

    def fake_replay(*args, **kwargs):
        replay_calls.append(kwargs.get("media_manifest"))
        return SimpleNamespace(as_dict=lambda: {"events_sent": 1})

    def fake_patch(result, **kwargs):
        patch_calls.append(result)
        return {"sent": 1}

    monkeypatch.setattr("lane_analytics.colab._process_one", fake_process_one)
    monkeypatch.setattr("lane_analytics.media.upload_evidence", fake_upload)
    monkeypatch.setattr("lane_analytics.evidence.generate_evidence", fake_evidence)
    monkeypatch.setattr("lane_analytics.colab.replay_run", fake_replay)
    monkeypatch.setattr("lane_analytics.colab.post_media_patch", fake_patch)

    queue = PostQueue(upload_workers=1)
    result = activate(
        source="drive_zip",
        zip_path=archive,
        video_dir=tmp_path / "raw",
        replay=True,
        webhook_url="https://lane-api.example",
        webhook_token="token",
        upload_media=True,
        post_queue=queue,
    )

    item = result.videos[0]
    assert item["status"] == "ok"

    # Replay is posted immediately; evidence and upload are still blocked.
    replay_deadline = time.monotonic() + 5
    while "replay" not in item and time.monotonic() < replay_deadline:
        time.sleep(0.01)
    assert item["replay"] == {"events_sent": 1}
    assert "media" not in item
    assert replay_calls == [None]

    release.set()
    queue.drain(timeout=10)

    assert item["media"] == {"uploaded": 1, "failed": 0}
    assert item["media_patch"] == {"sent": 1}
    assert len(patch_calls) == 1
