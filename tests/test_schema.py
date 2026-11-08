"""Contract tests for the lane event JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import validate

from lane_analytics.models import LaneEvent

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "schema" / "lane_event.schema.json"


def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_lane_enter_event_matches_schema():
    event = LaneEvent(
        camera_id="camera_01",
        timestamp="2026-11-07T08:00:00",
        track_id=7,
        object_class="car",
        confidence=0.91,
        event_type="lane_enter",
        lane_name="lane_left",
        lane_index=1,
        lane_role="normal",
        frame_index=10,
        video_time_s=1.0,
    )
    validate(event.to_dict(), schema())


def test_hogging_event_matches_schema():
    event = LaneEvent(
        camera_id="camera_01",
        timestamp="2026-11-07T08:00:12",
        track_id=12,
        object_class="truck",
        confidence=0.88,
        event_type="lane_hogging_indication",
        lane_name="lane_right",
        lane_index=3,
        lane_role="overtaking",
        previous_lane="",
        dwell_seconds=4.2,
        class_id=7,
        frame_index=120,
        video_time_s=12.0,
        severity="high",
        rule_name="heavy_vehicle_in_overtaking_lane",
        message="Truk menggunakan lajur overtaking.",
    )
    validate(event.to_dict(), schema())


def test_lane_event_without_index_matches_schema():
    event = LaneEvent(
        camera_id="camera_01",
        timestamp="2026-11-07T08:00:00",
        track_id=1,
        object_class="bus",
        confidence=0.77,
        event_type="lane_exit",
        lane_name="lane_center",
        dwell_seconds=2.5,
    )
    validate(event.to_dict(), schema())

def test_session_aware_event_matches_schema():
    event = LaneEvent(
        camera_id="camera_01",
        timestamp="2026-11-07T08:00:00",
        track_id=7,
        object_class="car",
        confidence=0.91,
        event_type="lane_enter",
        lane_name="lane_left",
        lane_index=1,
        lane_role="normal",
        frame_index=10,
        video_time_s=1.0,
        schema_version="1.1",
        session_id="live_20261108_082400",
        window_index=0,
        window_count=3,
    )
    validate(event.to_dict(), schema())
