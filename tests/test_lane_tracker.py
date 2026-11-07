"""Unit tests for lane occupancy, lane changes, and hogging indications."""

from __future__ import annotations

from lane_analytics.geometry import LaneRegion
from lane_analytics.hogging import HoggingRule
from lane_analytics.lane_tracker import LaneTracker
from lane_analytics.models import TrackedObject

LANES = [
    LaneRegion("lane_left", 1, "normal", "south", ((0.0, 0.0), (0.4, 0.0), (0.4, 1.0), (0.0, 1.0))),
    LaneRegion("lane_right", 2, "overtaking", "south", ((0.6, 0.0), (1.0, 0.0), (1.0, 1.0), (0.6, 1.0))),
]

CLASS_IDS = {"car": 2, "bus": 5, "truck": 7}


def make_object(
    track_id: int,
    x: float,
    *,
    class_name: str = "car",
    confidence: float = 0.9,
) -> TrackedObject:
    return TrackedObject(
        track_id=track_id,
        class_id=CLASS_IDS.get(class_name, 2),
        class_name=class_name,
        confidence=confidence,
        bbox=(x - 10, 80, x + 10, 100),
    )


def make_tracker(**kwargs) -> LaneTracker:
    defaults = dict(
        frame_width=100,
        frame_height=100,
        camera_id="camera_01",
        min_lane_frames=2,
        min_confidence=0.3,
        max_track_age_frames=90,
    )
    defaults.update(kwargs)
    return LaneTracker(LANES, **defaults)


def update(
    tracker: LaneTracker,
    x: float,
    frame: int,
    *,
    track_id: int = 1,
    class_name: str = "car",
    confidence: float = 0.9,
):
    return tracker.update(
        [make_object(track_id, x, class_name=class_name, confidence=confidence)],
        frame_index=frame,
        timestamp=f"2026-11-07T08:00:{frame % 60:02d}",
        video_time_s=frame * 0.1,
    )


def test_lane_enter_and_exit_on_finalize():
    tracker = make_tracker()
    assert update(tracker, 20.0, 1) == []
    events = update(tracker, 20.0, 2)
    assert [event.event_type for event in events] == ["lane_enter"]
    assert events[0].lane_name == "lane_left"
    assert events[0].lane_index == 1

    final = tracker.finalize(
        frame_index=3,
        timestamp="2026-11-07T08:00:03",
        video_time_s=0.3,
    )
    assert [event.event_type for event in final] == ["lane_exit"]
    assert final[0].dwell_seconds == 0.1  # last observation was frame 2

    summary = tracker.track_summaries()[0]
    assert len(summary.lane_stints) == 1
    assert summary.dominant_lane == "lane_left"
    assert summary.lane_change_count == 0


def test_lane_change_emits_exit_change_and_enter():
    tracker = make_tracker()
    update(tracker, 20.0, 1)
    update(tracker, 20.0, 2)
    assert update(tracker, 80.0, 3) == []  # pending, not stable yet
    events = update(tracker, 80.0, 4)

    assert [event.event_type for event in events] == ["lane_exit", "lane_change", "lane_enter"]
    assert events[0].lane_name == "lane_left"
    assert events[0].dwell_seconds == 0.2
    assert events[1].previous_lane == "lane_left"
    assert events[1].lane_name == "lane_right"
    assert events[2].lane_name == "lane_right"

    summary = tracker.track_summaries()
    tracker.finalize(frame_index=5, timestamp="2026-11-07T08:00:05", video_time_s=0.5)
    summary = tracker.track_summaries()[0]
    assert summary.lane_change_count == 1
    assert len(summary.lane_stints) == 2
    assert summary.dominant_lane == "lane_left"


def test_one_frame_flicker_does_not_create_lane_change():
    tracker = make_tracker()
    update(tracker, 20.0, 1)
    update(tracker, 20.0, 2)
    update(tracker, 80.0, 3)  # pending
    events = update(tracker, 20.0, 4)  # returned to the current lane
    assert events == []
    assert tracker.lane_change_count == 0


def test_hogging_indication_after_threshold():
    rule = HoggingRule(
        name="truck_in_overtaking_lane",
        lanes=frozenset(),
        lane_roles=frozenset({"overtaking"}),
        classes=frozenset({"truck"}),
        min_dwell_seconds=1.0,
        severity="high",
        description="truck stayed too long",
    )
    tracker = make_tracker(hogging_rules=[rule], hogging_enabled=True)

    for frame in range(1, 11):
        events = update(tracker, 80.0, frame, class_name="truck")
        assert not [event for event in events if event.event_type == "lane_hogging_indication"]

    events = update(tracker, 80.0, 11, class_name="truck")
    hogging = [event for event in events if event.event_type == "lane_hogging_indication"]
    assert len(hogging) == 1
    assert hogging[0].rule_name == "truck_in_overtaking_lane"
    assert hogging[0].severity == "high"
    assert hogging[0].dwell_seconds >= 1.0
    assert tracker.hogging_count == 1

    status = tracker.status_by_track()[1]
    assert status["hogging"] is True
    assert status["severity"] == "high"
    assert tracker.snapshot()["hogging_indications"] == 1


def test_car_is_allowed_when_rule_targets_trucks_only():
    rule = HoggingRule(
        name="truck_in_overtaking_lane",
        lanes=frozenset(),
        lane_roles=frozenset({"overtaking"}),
        classes=frozenset({"truck"}),
        min_dwell_seconds=0.5,
        severity="high",
    )
    tracker = make_tracker(hogging_rules=[rule])
    for frame in range(1, 20):
        update(tracker, 80.0, frame, class_name="car")
    assert tracker.hogging_count == 0
    assert tracker.status_by_track()[1]["hogging"] is False


def test_hogging_is_emitted_once_per_stint_by_default():
    rule = HoggingRule(
        name="truck_in_overtaking_lane",
        lanes=frozenset(),
        lane_roles=frozenset({"overtaking"}),
        classes=frozenset({"truck"}),
        min_dwell_seconds=0.5,
        severity="high",
    )
    tracker = make_tracker(hogging_rules=[rule])
    for frame in range(1, 20):
        update(tracker, 80.0, frame, class_name="truck")
    assert tracker.hogging_count == 1
    assert tracker.snapshot()["hogging_indications"] == 1


def test_hogging_can_retrigger_after_interval():
    rule = HoggingRule(
        name="truck_in_overtaking_lane",
        lanes=frozenset(),
        lane_roles=frozenset({"overtaking"}),
        classes=frozenset({"truck"}),
        min_dwell_seconds=0.5,
        severity="high",
    )
    tracker = make_tracker(hogging_rules=[rule], retrigger_after_seconds=1.0)
    for frame in range(1, 25):
        update(tracker, 80.0, frame, class_name="truck")
    assert tracker.hogging_count >= 2


def test_low_confidence_objects_are_ignored():
    tracker = make_tracker()
    update(tracker, 20.0, 1, confidence=0.1)
    events = update(tracker, 20.0, 2, confidence=0.1)
    assert events == []
    assert tracker.snapshot()["tracks_seen"] == 0


def test_single_misclassified_frame_does_not_trigger_heavy_vehicle_rule():
    rule = HoggingRule(
        name="heavy_in_overtaking_lane",
        lanes=frozenset(),
        lane_roles=frozenset({"overtaking"}),
        classes=frozenset({"truck", "bus"}),
        min_dwell_seconds=0.5,
        severity="high",
    )
    tracker = make_tracker(hogging_rules=[rule])

    for frame in range(1, 20):
        class_name = "bus" if frame == 10 else "car"
        events = update(tracker, 80.0, frame, class_name=class_name)
        hogging = [event for event in events if event.event_type == "lane_hogging_indication"]
        assert hogging == []

    assert tracker.hogging_count == 0
    final = tracker.finalize(frame_index=20, timestamp="2026-11-07T08:00:20", video_time_s=2.0)
    assert all(event.object_class == "car" for event in final)
    summary = tracker.track_summaries()[0]
    assert summary.object_class == "car"
    assert summary.class_id == 2



def test_stale_track_is_finalized_and_summarized():
    tracker = make_tracker(max_track_age_frames=2)
    update(tracker, 20.0, 1)
    update(tracker, 20.0, 2)
    events = tracker.update(
        [],
        frame_index=10,
        timestamp="2026-11-07T08:00:10",
        video_time_s=1.0,
    )
    assert [event.event_type for event in events] == ["lane_exit"]
    assert len(tracker.track_summaries()) == 1
    assert tracker.snapshot()["active_tracks"] == 0


def test_anchor_center_can_be_selected():
    tracker = make_tracker(anchor="center")
    # The bbox centre is above the bottom; both are inside the same lane here.
    update(tracker, 20.0, 1)
    events = update(tracker, 20.0, 2)
    assert [event.event_type for event in events] == ["lane_enter"]
