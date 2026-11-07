"""End-to-end smoke test for the synthetic lane demo pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from lane_analytics.demo import run_demo


def test_demo_pipeline_runs_and_writes_artifacts(tmp_path: Path):
    result = run_demo(
        tmp_path,
        duration=12.0,
        fps=10,
        width=480,
        height=480,
        seed=7,
        frame_stride=1,
        preview_frame=60,
    )

    assert Path(result.input_video).exists()
    assert Path(result.manifest_path).exists()
    assert result.run.output_video is not None and Path(result.run.output_video).exists()
    assert Path(result.run.events_path).exists()
    assert Path(result.run.tracks_path).exists()
    assert result.run.reports_dir is not None

    reports = Path(result.run.reports_dir)
    for name in (
        "summary.json",
        "summary.md",
        "events.csv",
        "vehicles.csv",
        "lane_stints.csv",
        "lane_usage.csv",
        "lane_changes.csv",
        "hogging_events.csv",
    ):
        assert (reports / name).exists(), name

    assert result.run.frames_processed == 120
    assert result.run.total_vehicles > 0
    assert result.run.total_events > 0
    assert result.run.hogging_events > 0
    assert result.run.summary["total_events"] == result.run.total_events
    assert result.run.summary["total_vehicles"] == result.run.total_vehicles
    assert result.run.summary["lane_usage"]
    assert result.preview_path is not None and Path(result.preview_path).exists()


def test_demo_pipeline_start_frame_window(tmp_path: Path):
    """start_frame seeks into the clip but keeps the original video timeline."""
    result = run_demo(
        tmp_path,
        duration=12.0,
        fps=10,
        width=480,
        height=480,
        seed=7,
        frame_stride=1,
        preview_frame=90,
        start_frame=30,
    )

    assert result.run.start_frame == 30
    assert result.run.frames_read == 90
    assert result.run.frames_processed == 90
    assert result.run.duration_s == pytest.approx(9.0)
    assert result.run.total_events > 0
    assert all(event.video_time_s >= 3.0 for event in result.run.events)
    assert all(event.timestamp >= "2026-11-07T08:00:03" for event in result.run.events)
    assert result.run.summary["run"]["start_frame"] == 30
