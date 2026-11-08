"""Tests for bbox-based lane assignment, overtake exemption, and rule tuning."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from lane_analytics.geometry import LaneRegion
from lane_analytics.hogging import HoggingRule
from lane_analytics.lane_tracker import LaneTracker
from lane_analytics.models import TrackedObject
from lane_analytics.tuning import (
    GroundTruthVehicle,
    Params,
    _score,
    evaluate_track_summaries,
    load_ground_truth,
)

LANES = [
    LaneRegion("lane_1", 1, "normal", "southbound", ((0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0))),
    LaneRegion("lane_2", 2, "normal", "southbound", ((0.5, 0.0), (1.0, 0.0), (1.0, 1.0), (0.5, 1.0))),
]

RULES = [
    HoggingRule(
        name="cruising_lane_2",
        lanes=frozenset(),
        lane_roles=frozenset(),
        lane_indices=frozenset({2}),
        classes=frozenset({"car", "truck", "bus"}),
        min_dwell_seconds=0.0,
        exempt_when_overtaking=True,
    )
]


def make_object(track_id: int, x1: float, x2: float, bottom_y: float, **kwargs) -> TrackedObject:
    class_name = kwargs.pop("class_name", "car")
    confidence = kwargs.pop("confidence", 0.9)
    class_ids = {"car": 2, "bus": 5, "truck": 7}
    return TrackedObject(
        track_id=track_id,
        class_id=class_ids.get(class_name, 2),
        class_name=class_name,
        confidence=confidence,
        bbox=(x1, bottom_y - 10.0, x2, bottom_y),
    )


def make_tracker(**kwargs) -> LaneTracker:
    defaults = dict(
        frame_width=100,
        frame_height=100,
        camera_id="cam",
        min_lane_frames=2,
        min_confidence=0.3,
        hogging_enabled=True,
        hogging_rules=RULES,
        overtake_exemption=True,
        overtake_gap_fraction=0.20,
        overtake_speed_margin_px_s=3.0,
        overtake_confirm_seconds=0.0,
    )
    defaults.update(kwargs)
    return LaneTracker(LANES, **defaults)


def test_bbox_vote_assigns_wide_box_to_majority_lane():
    tracker = make_tracker(assignment="bbox_vote", bbox_vote_samples=5, bbox_vote_min_share=0.5)
    # Box spans x=42..64: 3 of 5 bottom-edge samples fall in lane 2 (x >= 0.5).
    for index in range(3):
        tracker.update(
            [make_object(1, 42, 64, 60.0 + index)],
            frame_index=index,
            timestamp="",
            video_time_s=index * 0.05,
        )
    status = tracker.status_by_track()[1]
    assert status["lane"] == "lane_2"


def test_bbox_vote_respects_min_share():
    tracker = make_tracker(
        assignment="bbox_vote",
        bbox_vote_samples=9,
        bbox_vote_min_share=0.9,
        anchor="bottom_center",
    )
    # 6 of 9 samples in lane 1, 3 in lane 2: below the 0.9 share, fall back to the
    # bottom-centre anchor (x = 0.40) which still lands in lane 1.
    for index in range(3):
        tracker.update(
            [make_object(1, 20, 60, 60.0 + index)],
            frame_index=index,
            timestamp="",
            video_time_s=index * 0.05,
        )
    assert tracker.status_by_track()[1]["lane"] == "lane_1"


def test_cruising_lane_2_is_hogging():
    tracker = make_tracker()
    events = []
    for index in range(3):
        events.extend(
            tracker.update(
                [make_object(1, 70, 90, 60.0 + 4 * index)],
                frame_index=index,
                timestamp="",
                video_time_s=index * 0.05,
            )
        )
    assert [event.event_type for event in events].count("lane_hogging_indication") == 1


def test_passing_slower_inner_lane_vehicle_is_exempt():
    tracker = make_tracker()
    events = []
    for index in range(3):
        objects = [
            make_object(1, 70, 90, 60.0 + 4 * index),   # ego, faster
            make_object(2, 10, 30, 75.0 + 1 * index),   # target ahead in lane 1, slower
        ]
        events.extend(
            tracker.update(
                objects,
                frame_index=index,
                timestamp="",
                video_time_s=index * 0.05,
            )
        )
    assert not [event for event in events if event.event_type == "lane_hogging_indication"]
    assert tracker.status_by_track()[1]["overtaking"] is True


def test_confidence_weighted_class_vote():
    tracker = make_tracker()
    frames = [
        make_object(1, 70, 90, 60.0, class_name="car", confidence=0.95),
        make_object(1, 70, 90, 61.0, class_name="bus", confidence=0.31),
        make_object(1, 70, 90, 62.0, class_name="bus", confidence=0.31),
        make_object(1, 70, 90, 63.0, class_name="bus", confidence=0.31),
    ]
    for index, obj in enumerate(frames):
        tracker.update([obj], frame_index=index, timestamp="", video_time_s=index * 0.05)
    tracker.finalize(frame_index=3, timestamp="", video_time_s=0.15)
    assert tracker.track_summaries()[0].object_class == "car"


def test_stint_evaluation_defers_hogging_until_lane_exit():
    tracker = make_tracker(hogging_evaluate_at="stint", min_lane_frames=1)
    events = []
    for index in range(4):
        events.extend(
            tracker.update(
                [make_object(1, 70, 90, 60.0 + 2 * index)],
                frame_index=index,
                timestamp="",
                video_time_s=index * 0.1,
            )
        )
    assert not [event for event in events if event.event_type == "lane_hogging_indication"]
    events.extend(tracker.finalize(frame_index=3, timestamp="", video_time_s=0.3))
    indications = [event for event in events if event.event_type == "lane_hogging_indication"]
    assert len(indications) == 1
    assert indications[0].dwell_seconds == pytest.approx(0.3, abs=0.05)


def test_stint_evaluation_exempts_a_completed_pass():
    tracker = make_tracker(
        hogging_evaluate_at="stint", min_lane_frames=1, overtake_stint_memory=True
    )
    for index in range(4):
        tracker.update(
            [
                make_object(1, 70, 90, 60.0 + 4 * index),
                make_object(2, 10, 30, 75.0 + 1 * index),
            ],
            frame_index=index,
            timestamp="",
            video_time_s=index * 0.05,
        )
    events = tracker.finalize(frame_index=3, timestamp="", video_time_s=0.15)
    assert not [event for event in events if event.event_type == "lane_hogging_indication"]


def test_load_ground_truth_groups_split_ids(tmp_path: Path):
    path = tmp_path / "labels.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["vehicle_key", "track_id", "direction", "object_class", "lane_index", "hogging", "reason"])
        writer.writerow(["542/565", 542, "northbound", "car", 3, "true", "Cruising"])
        writer.writerow(["542/565", 565, "northbound", "car", 3, "true", "Cruising"])
        writer.writerow(["31", 31, "southbound", "truck", 2, "false", "Overtaking"])
    labels = load_ground_truth(path)
    assert [item.key for item in labels] == ["31", "542/565"]
    split = labels[1]
    assert split.track_ids == (542, 565)
    assert split.hogging is True


def test_score_counts_split_ids_as_one_vehicle():
    labels = [
        GroundTruthVehicle("542/565", (542, 565), "northbound", "car", 3, True),
        GroundTruthVehicle("31", (31,), "southbound", "truck", 2, False),
    ]
    metrics, predictions, excluded = _score(labels, predicted_ids={565})
    assert (metrics.tp, metrics.fp, metrics.fn) == (1, 0, 0)
    assert excluded == []
    assert {item.outcome for item in predictions} == {"TP", "TN"}


def test_proxy_evaluation_exempts_overlap_but_flags_cruising():
    summaries = [
        {
            "track_id": 10,
            "object_class": "car",
            "hogging_event_count": 0,
            "lane_stints": [
                {"lane_name": "sb_lane_3", "lane_index": 3, "enter_time_s": 0.0, "exit_time_s": 5.0, "dwell_seconds": 5.0}
            ],
        },
        {
            "track_id": 11,
            "object_class": "truck",
            "hogging_event_count": 0,
            "lane_stints": [
                {"lane_name": "sb_lane_2", "lane_index": 2, "enter_time_s": 0.0, "exit_time_s": 6.0, "dwell_seconds": 6.0}
            ],
        },
        {
            "track_id": 12,
            "object_class": "car",
            "hogging_event_count": 1,
            "lane_stints": [
                {"lane_name": "nb_lane_3", "lane_index": 3, "enter_time_s": 0.0, "exit_time_s": 4.0, "dwell_seconds": 4.0}
            ],
        },
    ]
    labels = [
        GroundTruthVehicle("10", (10,), "southbound", "car", 3, False),   # overtaking #11
        GroundTruthVehicle("11", (11,), "southbound", "truck", 2, True),  # cruising
        GroundTruthVehicle("12", (12,), "northbound", "car", 3, True),    # cruising
    ]
    outcome = evaluate_track_summaries(
        summaries, labels, Params(heavy_dwell=0.4, car_dwell=0.4, overlap_ratio_max=0.8)
    )
    outcomes = {item.key: item.outcome for item in outcome.predictions}
    assert outcomes == {"10": "TN", "11": "TP", "12": "TP"}
    assert outcome.metrics.fp == 0
