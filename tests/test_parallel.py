"""Unit tests for parallel segmentation, handover matching, and frame replays."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from lane_analytics.config import (
    AppConfig,
    EvidenceConfig,
    HoggingConfig,
    LaneConfig,
    OccupancyConfig,
    OutputConfig,
    VideoConfig,
)
from lane_analytics.models import TrackedObject
from lane_analytics.parallel import (
    FrameLogObservationSource,
    Segment,
    assign_first_segment_ids,
    assign_next_segment_ids,
    bbox_iou,
    build_local_tracks,
    hungarian_assignment,
    is_gpu_device,
    match_overlap_tracks,
    merge_segment_frame_logs,
    plan_segments,
    read_frame_log,
    resolve_worker_devices,
    worker_pool_context,
)
from lane_analytics.pipeline import process_video


def _observation(frame: int, track_id: int, *, x: float = 100.0, y: float = 200.0):
    from lane_analytics.parallel import FrameObservation

    return FrameObservation(
        frame_index=frame,
        timestamp=f"2026-01-01T00:00:{frame:02d}",
        video_time_s=float(frame) / 10.0,
        track_id=track_id,
        class_id=2,
        class_name="car",
        confidence=0.9,
        bbox=(x, y, x + 20.0, y + 40.0),
    )


def _record(frame: int, track_id: int, *, x: float = 100.0, y: float = 200.0, confidence: float = 0.9):
    return {
        "frame_index": frame,
        "timestamp": f"2026-01-01T00:00:{frame:02d}",
        "video_time_s": frame / 10.0,
        "objects": [
            {
                "track_id": track_id,
                "class_id": 2,
                "class_name": "car",
                "confidence": confidence,
                "bbox": [x, y, x + 20.0, y + 40.0],
            }
        ],
    }


# ---------------------------------------------------------------------- planning
def test_plan_segments_is_continuous_with_three_second_overlap():
    segments = plan_segments(3000, 30.0, segment_seconds=12.0, overlap_seconds=3.0)

    assert [segment.start_frame for segment in segments[:4]] == [0, 270, 540, 810]
    assert segments[0].end_frame == 360
    assert segments[-1].end_frame == 3000
    for earlier, later in zip(segments, segments[1:]):
        assert later.start_frame - earlier.start_frame == 270  # 9 seconds at 30 fps
        assert earlier.end_frame - later.start_frame == 90  # 3 seconds of overlap


def test_plan_segments_absorbs_a_short_tail():
    segments = plan_segments(640, 30.0, segment_seconds=12.0, overlap_seconds=3.0)

    assert len(segments) == 2
    assert segments[-1].end_frame == 640
    assert segments[-1].start_frame == 270


def test_plan_segments_honours_window():
    segments = plan_segments(
        3000, 30.0, segment_seconds=12.0, overlap_seconds=3.0, start_frame=300, max_frames=600
    )

    assert segments[0].start_frame == 300
    assert segments[-1].end_frame == 900
    assert all(segment.end_frame <= 900 for segment in segments)


def test_plan_segments_rejects_bad_overlap():
    with pytest.raises(ValueError):
        plan_segments(100, 30.0, segment_seconds=3.0, overlap_seconds=3.0)


def test_resolve_worker_devices_two_gpus():
    slots, workers = resolve_worker_devices(
        "0", workers=3, devices=["0", "1"], segment_count=10
    )
    assert slots == ["0", "1"]
    assert workers == 2


def test_resolve_worker_devices_collapses_duplicates():
    slots, workers = resolve_worker_devices(
        "cpu", workers=4, devices=["0", "0", "1"], segment_count=10
    )
    assert slots == ["0", "1"]
    assert workers == 2


def test_resolve_worker_devices_clamps_to_segments():
    slots, workers = resolve_worker_devices(
        "cpu", workers=8, devices=None, segment_count=3
    )
    assert slots == ["cpu", "cpu", "cpu"]
    assert workers == 3


def test_resolve_worker_devices_rejects_empty_list():
    with pytest.raises(ValueError):
        resolve_worker_devices("cpu", workers=2, devices=[], segment_count=10)


def test_is_gpu_device():
    assert not is_gpu_device("cpu")
    assert not is_gpu_device("")
    assert is_gpu_device("0")
    assert is_gpu_device("cuda:1")


def test_gpu_workers_use_spawn_context():
    assert worker_pool_context(["cpu"]) is None
    context = worker_pool_context(["0", "1"])
    assert context is not None
    assert context.get_start_method() == "spawn"


# ---------------------------------------------------------------------- geometry
def test_bbox_iou_identical_disjoint_and_partial():
    assert bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)
    assert bbox_iou((0, 0, 10, 10), (20, 20, 30, 30)) == pytest.approx(0.0)
    # Half of the first box overlaps half of the second.
    assert bbox_iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1.0 / 3.0)


def test_hungarian_assignment_identity_and_rectangular():
    assert hungarian_assignment([[1.0, 9.0], [9.0, 1.0]]) == [0, 1]
    assert hungarian_assignment([[9.0, 1.0, 9.0], [9.0, 9.0, 1.0]]) == [1, 2]


# ------------------------------------------------------------------- matching
def test_overlap_matching_links_shifted_boxes():
    earlier = {
        5: [_observation(frame, 5, x=100.0 + frame, y=300.0 - frame) for frame in range(100, 111)]
    }
    later = {
        # Same vehicle, different ByteTrack id, one pixel offset.
        2: [_observation(frame, 2, x=101.0 + frame, y=299.0 - frame) for frame in range(100, 111)],
        # A different vehicle passing on the other side of the frame.
        3: [_observation(frame, 3, x=600.0, y=100.0) for frame in range(100, 111)],
    }

    matches = match_overlap_tracks(earlier, later, overlap_start=100, overlap_end=111)

    assert len(matches) == 1
    earlier_id, later_id, mean_iou, best_iou, motion, common = matches[0]
    assert (earlier_id, later_id) == (5, 2)
    assert mean_iou > 0.7
    assert best_iou >= mean_iou - 1e-9
    assert motion > 0.9
    assert common == 11


def test_overlap_matching_rejects_opposite_motion():
    # They share a single frame, so IoU passes, but the headings are opposite.
    earlier = {5: [_observation(frame, 5, x=100.0, y=300.0 - frame) for frame in range(0, 11)]}
    later = {
        2: [_observation(frame, 2, x=100.0, y=290.0 + (frame - 10)) for frame in range(10, 21)]
    }

    assert match_overlap_tracks(earlier, later, overlap_start=0, overlap_end=21) == []


def test_overlap_matching_requires_spatial_overlap():
    earlier = {5: [_observation(frame, 5, x=0.0, y=0.0) for frame in range(10)]}
    later = {2: [_observation(frame, 2, x=500.0, y=500.0) for frame in range(10)]}

    assert match_overlap_tracks(earlier, later, overlap_start=0, overlap_end=10) == []


# --------------------------------------------------------------- canonical ids
def test_canonical_ids_chain_across_segments():
    previous = {
        5: [_observation(frame, 5, x=100.0, y=300.0 - frame) for frame in range(0, 11)],
        7: [_observation(frame, 7, x=500.0, y=100.0) for frame in range(2, 9)],
    }
    first_mapping, next_id = assign_first_segment_ids(previous)
    assert first_mapping == {5: 1, 7: 2}
    assert next_id == 3

    current = {
        # Local id 2 is the same vehicle as previous local id 5.
        2: [_observation(frame, 2, x=101.0, y=299.0 - frame) for frame in range(8, 20)],
        # Local id 9 is new.
        9: [_observation(frame, 9, x=700.0, y=80.0) for frame in range(8, 20)],
    }
    mapping, next_id, handovers = assign_next_segment_ids(
        previous_local_tracks=previous,
        current_local_tracks=current,
        previous_mapping=first_mapping,
        next_canonical_id=next_id,
        segment_index=1,
        overlap_start=8,
        overlap_end=11,
    )

    assert mapping[2] == 1
    assert mapping[9] == 3
    assert next_id == 4
    assert len(handovers) == 1
    assert handovers[0].earlier_track_id == 5
    assert handovers[0].later_track_id == 2
    assert handovers[0].canonical_track_id == 1


# ------------------------------------------------------------------ merging
def test_merge_segment_logs_dedupes_overlap_with_higher_confidence(tmp_path: Path):
    segments = [
        Segment(0, 0, 10, 12.0, 3.0),
        Segment(1, 7, 17, 12.0, 3.0),
    ]
    first = [_record(frame, 5, confidence=0.5) for frame in range(0, 10)]
    second = [_record(frame, 2, confidence=0.9) for frame in range(7, 17)]

    output = tmp_path / "merged.jsonl"
    written = merge_segment_frame_logs(
        [first, second],
        segments,
        [{5: 1}, {2: 1}],
        output,
    )

    records = read_frame_log(output)
    assert written == 17
    assert [record["frame_index"] for record in records] == list(range(17))
    by_frame = {record["frame_index"]: record for record in records}
    for frame in range(7, 10):
        assert by_frame[frame]["objects"][0]["confidence"] == pytest.approx(0.9)
    for frame in range(0, 7):
        assert by_frame[frame]["objects"][0]["confidence"] == pytest.approx(0.5)
    assert build_local_tracks(records)[1]


# --------------------------------------------------------------- frame replays
class _FakeDetector:
    name = "fake-detector"

    def update(self, frame: np.ndarray, frame_index: int) -> list[TrackedObject]:
        x = 40.0 + min(frame_index, 10) * 4.0
        return [
            TrackedObject(
                track_id=1,
                class_id=2,
                class_name="car",
                confidence=0.95,
                bbox=(x, 40.0, x + 30.0, 90.0),
            )
        ]


def _write_test_clip(path: Path, frames: int = 20, width: int = 160, height: int = 120) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (width, height)
    )
    assert writer.isOpened()
    for index in range(frames):
        image = np.full((height, width, 3), 40 + index, dtype=np.uint8)
        writer.write(image)
    writer.release()


def _test_config(video: Path, output: Path) -> AppConfig:
    return AppConfig(
        camera_id="test",
        base_timestamp="2026-01-01T00:00:00",
        video=VideoConfig(path=str(video), frame_stride=1),
        lanes=[
            LaneConfig(
                name="lane_1",
                index=1,
                direction="northbound",
                role="normal",
                polygon=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
            )
        ],
        occupancy=OccupancyConfig(anchor="bottom_center", min_lane_frames=2),
        hogging=HoggingConfig(enabled=False),
        evidence=EvidenceConfig(enabled=False),
        output=OutputConfig(dir=str(output)),
    )


def test_process_video_frame_limit_processes_one_window(tmp_path: Path):
    video = tmp_path / "clip.avi"
    _write_test_clip(video)
    config = _test_config(video, tmp_path / "out")

    result = process_video(
        config,
        detector=_FakeDetector(),
        video_path=video,
        events_path=tmp_path / "events.jsonl",
        tracks_path=tmp_path / "tracks.jsonl",
        output_video=tmp_path / "window.avi",
        write_video=False,
        generate_reports=False,
        run_id="window",
        start_frame=5,
        frame_limit=6,
    )

    assert result.start_frame == 5
    assert result.frames_processed == 6
    assert result.frames_read == 6
    assert result.duration_s == pytest.approx(0.6)
    assert all(event.video_time_s >= 0.5 for event in result.events)


def test_process_video_writes_frame_log_and_replays_it(tmp_path: Path):
    video = tmp_path / "clip.avi"
    _write_test_clip(video)
    config = _test_config(video, tmp_path / "out")
    frame_log = tmp_path / "frames.jsonl"

    first = process_video(
        config,
        detector=_FakeDetector(),
        video_path=video,
        events_path=tmp_path / "events_a.jsonl",
        tracks_path=tmp_path / "tracks_a.jsonl",
        output_video=tmp_path / "a.avi",
        write_video=False,
        generate_reports=False,
        run_id="first",
        frame_log_path=frame_log,
    )

    records = read_frame_log(frame_log)
    assert len(records) == 20
    assert all(record["objects"] for record in records)
    assert first.frames_processed == 20

    source = FrameLogObservationSource(frame_log)
    second = process_video(
        config,
        video_path=video,
        events_path=tmp_path / "events_b.jsonl",
        tracks_path=tmp_path / "tracks_b.jsonl",
        output_video=tmp_path / "b.avi",
        write_video=False,
        generate_reports=False,
        run_id="second",
        observation_source=source,
    )

    def comparable(events):
        return [
            {
                key: value
                for key, value in event.to_dict().items()
                if key != "event_id"
            }
            for event in events
        ]

    assert second.frames_processed == first.frames_processed
    assert second.total_events == first.total_events
    assert comparable(second.events) == comparable(first.events)

def test_process_video_propagates_session_window_metadata(tmp_path: Path):
    video = tmp_path / "clip.avi"
    _write_test_clip(video)
    config = _test_config(video, tmp_path / "out")

    result = process_video(
        config,
        detector=_FakeDetector(),
        video_path=video,
        events_path=tmp_path / "events.jsonl",
        tracks_path=tmp_path / "tracks.jsonl",
        output_video=tmp_path / "session.avi",
        write_video=False,
        generate_reports=False,
        run_id="sess_01_w0001",
        session_id="sess_01",
        window_index=1,
        window_count=3,
    )

    assert result.session_id == "sess_01"
    assert result.window_index == 1
    assert result.window_count == 3
    assert result.events
    assert all(event.session_id == "sess_01" for event in result.events)
    assert all(event.window_index == 1 for event in result.events)
    assert all(event.schema_version == "1.1" for event in result.events)
    assert result.tracks[0]["session_id"] == "sess_01"
    assert result.tracks[0]["window_count"] == 3
    assert result.summary["run"]["session_id"] == "sess_01"
