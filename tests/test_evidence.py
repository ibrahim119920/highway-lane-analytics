"""Tests for snapshot + clip evidence generation (synthetic video, no YOLO)."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from lane_analytics.evidence import (
    _ffmpeg_exe,
    clamp_window,
    event_frame,
    event_time_s,
    generate_evidence,
    probe_video,
    select_events,
)


def test_ffmpeg_exe_prefers_system_binary(monkeypatch):
    import lane_analytics.evidence as evidence

    monkeypatch.setattr(evidence.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert _ffmpeg_exe() == "/usr/bin/ffmpeg"


def test_ffmpeg_exe_falls_back_to_imageio(monkeypatch):
    import sys
    import types

    import lane_analytics.evidence as evidence

    monkeypatch.setattr(evidence.shutil, "which", lambda name: None)
    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        types.SimpleNamespace(get_ffmpeg_exe=lambda: "C:/fake/ffmpeg.exe"),
    )
    assert _ffmpeg_exe() == "C:/fake/ffmpeg.exe"


def test_ffmpeg_exe_returns_none_without_any_binary(monkeypatch):
    import sys

    import lane_analytics.evidence as evidence

    monkeypatch.setattr(evidence.shutil, "which", lambda name: None)
    # A None entry makes the import fail like a missing package.
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    assert _ffmpeg_exe() is None


def make_video(path: Path, *, seconds: int = 5, fps: int = 10, size=(160, 120)) -> Path:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size
    )
    assert writer.isOpened()
    for index in range(seconds * fps):
        frame = np.full((size[1], size[0], 3), index % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path


def make_event(
    time_s: float,
    event_type: str,
    *,
    track_id: int = 1,
    fps: int = 10,
    **extra,
) -> dict:
    event = {
        "camera_id": "cam_01",
        "event_id": f"{event_type}-{track_id}-{time_s}",
        "event_type": event_type,
        "track_id": track_id,
        "object_class": "car",
        "lane_name": "nb_lane_1",
        "timestamp": "2026-11-08T06:58:00",
        "video_time_s": time_s,
        "frame_index": int(round(time_s * fps)),
        "severity": "",
        "rule_name": "",
    }
    event.update(extra)
    return event


def write_events(path: Path, events: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    return path


def test_clamp_window_handles_edges():
    assert clamp_window(1.0, 1.0, 1.0, 5.0) == (0.0, 2.0)
    assert clamp_window(0.2, 1.0, 1.0, 5.0) == (0.0, 1.2)
    assert clamp_window(4.8, 1.0, 1.0, 5.0) == (3.8, 5.0)
    start, end = clamp_window(1.0, 0.0, 0.0, 5.0)
    assert end > start


def test_select_events_filters_types():
    events = [
        make_event(1.0, "lane_enter"),
        make_event(2.0, "lane_change"),
        make_event(3.0, "lane_hogging_indication"),
    ]
    assert len(select_events(events)) == 3
    selected = select_events(events, ("lane_change", "lane_hogging_indication"))
    assert [event["event_type"] for event in selected] == [
        "lane_change",
        "lane_hogging_indication",
    ]


def test_event_time_and_frame_fallbacks():
    event = {"frame_index": 25}
    assert event_time_s(event, 10.0) == 2.5
    assert event_frame(event, 10.0) == 25
    assert event_time_s({"video_time_s": 1.25}, 10.0) == 1.25


def test_probe_video_reads_duration(tmp_path: Path):
    video = make_video(tmp_path / "clip.mp4", seconds=3, fps=10)
    info = probe_video(video)
    assert info.width == 160
    assert info.height == 120
    assert info.fps == pytest.approx(10.0, rel=0.05)
    assert info.duration_s == pytest.approx(3.0, rel=0.1)


def test_generate_evidence_writes_snapshots_and_clips(tmp_path: Path):
    video = make_video(tmp_path / "clip.mp4", seconds=5, fps=10)
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            make_event(1.0, "lane_change"),
            make_event(1.0, "lane_enter"),  # same frame, filtered out below
            make_event(3.0, "lane_hogging_indication", severity="medium"),
        ],
    )

    result = generate_evidence(
        run_id="run_a",
        events_path=events_path,
        video_path=video,
        output_dir=tmp_path / "evidence",
        camera_id="cam_01",
        event_types=("lane_change", "lane_hogging_indication"),
        before_seconds=1.0,
        after_seconds=1.0,
        use_ffmpeg=False,
    )

    assert result.event_count == 2
    assert result.snapshot_count == 2
    assert result.clip_count == 2
    assert Path(result.index_path).exists()

    index = json.loads(Path(result.index_path).read_text(encoding="utf-8"))
    assert index["event_count"] == 2
    assert index["event_types"] == ["lane_change", "lane_hogging_indication"]
    for entry in index["events"]:
        assert entry["snapshot"]
        assert entry["clip"]
        assert (tmp_path / "evidence" / entry["snapshot"]).exists()
        assert (tmp_path / "evidence" / entry["clip"]).stat().st_size > 0
        assert entry["clip_end_s"] - entry["clip_start_s"] == pytest.approx(2.0)


def test_generate_evidence_shares_files_for_same_frame(tmp_path: Path):
    video = make_video(tmp_path / "clip.mp4", seconds=4, fps=10)
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            make_event(1.0, "lane_exit"),
            make_event(1.0, "lane_change"),
            make_event(1.0, "lane_enter"),
        ],
    )

    result = generate_evidence(
        run_id="run_same",
        events_path=events_path,
        video_path=video,
        output_dir=tmp_path / "evidence",
        before_seconds=1.0,
        after_seconds=1.0,
        use_ffmpeg=False,
    )

    assert result.event_count == 3
    assert result.snapshot_count == 1
    assert result.clip_count == 1
    index = json.loads(Path(result.index_path).read_text(encoding="utf-8"))
    assert len({entry["snapshot"] for entry in index["events"]}) == 1
    assert len({entry["clip"] for entry in index["events"]}) == 1


def test_generate_evidence_respects_max_clips(tmp_path: Path):
    video = make_video(tmp_path / "clip.mp4", seconds=5, fps=10)
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            make_event(0.5, "lane_enter"),
            make_event(2.0, "lane_exit"),
            make_event(3.5, "lane_enter"),
        ],
    )

    result = generate_evidence(
        run_id="run_cap",
        events_path=events_path,
        video_path=video,
        output_dir=tmp_path / "evidence",
        before_seconds=0.5,
        after_seconds=0.5,
        max_clips=1,
        use_ffmpeg=False,
    )

    assert result.event_count == 3
    assert result.clip_count == 1
    index = json.loads(Path(result.index_path).read_text(encoding="utf-8"))
    assert sum(1 for entry in index["events"] if entry["clip"]) == 1


def test_generate_evidence_uses_time_offset(tmp_path: Path):
    video = make_video(tmp_path / "clip.mp4", seconds=3, fps=10)
    events_path = write_events(
        tmp_path / "events.jsonl", [make_event(12.0, "lane_enter")]
    )

    result = generate_evidence(
        run_id="run_offset",
        events_path=events_path,
        video_path=video,
        output_dir=tmp_path / "evidence",
        before_seconds=1.0,
        after_seconds=1.0,
        time_offset_s=10.0,
        use_ffmpeg=False,
    )

    assert result.snapshot_count == 1
    assert result.clip_count == 1
    index = json.loads(Path(result.index_path).read_text(encoding="utf-8"))
    entry = index["events"][0]
    assert index["video"]["time_offset_s"] == 10.0
    assert entry["video_time_s"] == 12.0
    assert entry["clip_start_s"] == 11.0
    assert entry["clip_end_s"] == 13.0
    assert entry["clip_local_start_s"] == 1.0
    assert entry["clip_local_end_s"] == 3.0
    assert (tmp_path / "evidence" / entry["snapshot"]).exists()
    assert (tmp_path / "evidence" / entry["clip"]).exists()


def test_generate_evidence_skips_events_outside_video_window(tmp_path: Path):
    video = make_video(tmp_path / "clip.mp4", seconds=3, fps=10)
    events_path = write_events(
        tmp_path / "events.jsonl",
        [make_event(12.0, "lane_enter"), make_event(30.0, "lane_exit")],
    )

    result = generate_evidence(
        run_id="run_outside",
        events_path=events_path,
        video_path=video,
        output_dir=tmp_path / "evidence",
        time_offset_s=10.0,
        use_ffmpeg=False,
    )

    assert result.event_count == 2
    assert result.skipped_events == 1
    assert result.snapshot_count == 1
    assert result.clip_count == 1


def test_generate_evidence_skips_events_without_time(tmp_path: Path):
    video = make_video(tmp_path / "clip.mp4", seconds=2, fps=10)
    events_path = write_events(
        tmp_path / "events.jsonl",
        [{"event_type": "lane_enter", "camera_id": "cam_01"}],
    )
    result = generate_evidence(
        run_id="run_none",
        events_path=events_path,
        video_path=video,
        output_dir=tmp_path / "evidence",
        use_ffmpeg=False,
    )
    assert result.event_count == 1  # selected, but it has no usable time
    assert result.skipped_events == 1
    assert result.snapshot_count == 0
    assert result.clip_count == 0


def test_generate_evidence_parallel_workers_match_sequential(tmp_path: Path):
    video = make_video(tmp_path / "clip.mp4", seconds=5, fps=10)
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            make_event(0.7, "lane_enter"),
            make_event(1.9, "lane_enter"),
            make_event(3.1, "lane_enter"),
            make_event(4.3, "lane_enter"),
        ],
    )

    sequential = generate_evidence(
        run_id="run_seq",
        events_path=events_path,
        video_path=video,
        output_dir=tmp_path / "seq",
        before_seconds=0.5,
        after_seconds=0.5,
        use_ffmpeg=False,
        clip_workers=1,
    )
    parallel = generate_evidence(
        run_id="run_par",
        events_path=events_path,
        video_path=video,
        output_dir=tmp_path / "par",
        before_seconds=0.5,
        after_seconds=0.5,
        use_ffmpeg=False,
        clip_workers=4,
    )

    assert parallel.event_count == sequential.event_count == 4
    assert parallel.snapshot_count == sequential.snapshot_count == 4
    assert parallel.clip_count == sequential.clip_count == 4
    assert parallel.errors == []
    seq_index = json.loads(Path(sequential.index_path).read_text(encoding="utf-8"))
    par_index = json.loads(Path(parallel.index_path).read_text(encoding="utf-8"))
    assert [entry["clip"] for entry in seq_index["events"]] == [
        entry["clip"] for entry in par_index["events"]
    ]
