"""End-to-end smoke test for the synthetic lane demo pipeline."""

from __future__ import annotations

from pathlib import Path

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
