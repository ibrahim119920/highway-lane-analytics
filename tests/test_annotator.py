"""Tests for annotation layout helpers (no detection or video decoding)."""

from __future__ import annotations

import numpy as np

from lane_analytics.annotator import Annotator
from lane_analytics.geometry import LaneRegion
from lane_analytics.models import TrackedObject


def make_lane(*, name: str = "nb_lane_1", index: int = 1, direction: str = "northbound"):
    return LaneRegion(
        name=name,
        index=index,
        role="normal",
        direction=direction,
        points=((0.10, 0.30), (0.20, 0.30), (0.30, 0.90), (0.00, 0.90)),
    )


def test_lane_label_anchor_is_top_edge_midpoint():
    points = [(10, 100), (30, 100), (40, 200), (0, 200)]
    assert Annotator._lane_label_anchor(points) == (20, 100)


def test_lane_label_sits_above_the_polygon():
    frame = np.zeros((200, 300, 3), dtype=np.uint8)
    annotator = Annotator([make_lane()], "cam_01")
    annotator.draw_lanes(frame)

    # The label is staggered above the horizon, so the polygon centroid must not
    # be covered by the opaque label background.
    centroid_pixel = frame[90, 45]
    assert not np.array_equal(centroid_pixel, np.array([20, 20, 20], dtype=np.uint8))


def test_translucent_fill_keeps_underlying_pixels_visible():
    frame = np.full((20, 20, 3), 255, dtype=np.uint8)
    Annotator._fill_rect_alpha(frame, (5, 5), (15, 15), (0, 0, 0), 0.5)

    # 50% black over white must be grey, not pure black.
    assert 126 <= int(frame[10, 10, 0]) <= 129


def test_boxes_are_drawn_before_labels(monkeypatch):
    frame = np.zeros((200, 300, 3), dtype=np.uint8)
    annotator = Annotator([make_lane()], "cam_01")
    objects = [
        TrackedObject(
            track_id=track_id,
            class_id=2,
            class_name="car",
            confidence=0.9,
            bbox=(100.0, 100.0, 180.0, 160.0),
        )
        for track_id in (7, 8)
    ]
    calls: list[str] = []
    monkeypatch.setattr(annotator, "_draw_box", lambda *args, **kwargs: calls.append("box"))
    monkeypatch.setattr(annotator, "_draw_label", lambda *args, **kwargs: calls.append("label"))

    annotator.draw(
        frame,
        objects,
        frame_index=0,
        timestamp="2026-11-08T06:58:00",
        status_by_track={},
        counters={},
    )

    assert calls == ["box", "box", "label", "label"]
