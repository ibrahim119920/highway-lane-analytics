"""Unit tests for tracked-object anchor selection."""

from __future__ import annotations

from lane_analytics.models import TrackedObject


def make_object(bbox: tuple[float, float, float, float]) -> TrackedObject:
    return TrackedObject(
        track_id=1,
        class_id=7,
        class_name="truck",
        confidence=0.9,
        bbox=bbox,
    )


def test_directional_anchor_keeps_bottom_centre_for_small_boxes():
    obj = make_object((40.0, 80.0, 60.0, 100.0))  # width 20 < small threshold
    assert obj.directional_anchor("northbound", small_px=40, large_px=80) == (50.0, 100.0)
    assert obj.directional_anchor("southbound", small_px=40, large_px=80) == (50.0, 100.0)


def test_directional_anchor_slides_to_the_outer_corner_for_wide_boxes():
    obj = make_object((10.0, 60.0, 90.0, 100.0))  # width 80 >= large threshold
    assert obj.directional_anchor("northbound", small_px=40, large_px=80) == (10.0, 100.0)
    assert obj.directional_anchor("southbound", small_px=40, large_px=80) == (90.0, 100.0)


def test_directional_anchor_interpolates_between_thresholds():
    obj = make_object((20.0, 60.0, 80.0, 100.0))  # width 60 -> halfway
    assert obj.directional_anchor("southbound", small_px=40, large_px=80) == (65.0, 100.0)
    assert obj.directional_anchor("northbound", small_px=40, large_px=80) == (35.0, 100.0)


def test_directional_anchor_falls_back_without_a_direction():
    obj = make_object((20.0, 60.0, 80.0, 100.0))
    assert obj.directional_anchor("", small_px=40, large_px=80) == (50.0, 100.0)
    assert obj.directional_anchor(
        "", small_px=40, large_px=80, fallback="bottom_right"
    ) == (80.0, 100.0)


def test_directional_anchor_handles_inverted_thresholds():
    obj = make_object((20.0, 60.0, 80.0, 100.0))  # width 60
    assert obj.directional_anchor("southbound", small_px=80, large_px=40) == (80.0, 100.0)
