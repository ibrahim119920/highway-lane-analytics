"""Unit tests for polygon geometry and lane assignment."""

from __future__ import annotations

from lane_analytics.geometry import (
    LaneRegion,
    assign_lane,
    point_in_polygon,
    polygon_area,
    polygon_centroid,
)

SQUARE = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


def test_point_inside_and_outside_polygon():
    assert point_in_polygon((0.5, 0.5), SQUARE)
    assert not point_in_polygon((1.5, 0.5), SQUARE)
    assert not point_in_polygon((-0.1, 0.5), SQUARE)


def test_boundary_point_counts_as_inside():
    assert point_in_polygon((0.0, 0.5), SQUARE)
    assert point_in_polygon((1.0, 0.5), SQUARE)
    assert point_in_polygon((0.5, 0.0), SQUARE)


def test_polygon_area_and_centroid():
    assert polygon_area(SQUARE) == 1.0
    assert polygon_centroid(SQUARE) == (0.5, 0.5)


def test_lane_region_assignment():
    left = LaneRegion("left", 1, "normal", "south", ((0.0, 0.0), (0.4, 0.0), (0.4, 1.0), (0.0, 1.0)))
    right = LaneRegion("right", 2, "overtaking", "south", ((0.6, 0.0), (1.0, 0.0), (1.0, 1.0), (0.6, 1.0)))
    lanes = [left, right]

    assert assign_lane((0.2, 0.5), lanes) is left
    assert assign_lane((0.8, 0.5), lanes) is right
    assert assign_lane((0.5, 0.5), lanes) is None


def test_overlapping_polygons_choose_smallest_area():
    large = LaneRegion("large", 1, "normal", "south", ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)))
    small = LaneRegion("small", 2, "normal", "south", ((0.2, 0.2), (0.4, 0.2), (0.4, 0.4), (0.2, 0.4)))
    assert assign_lane((0.3, 0.3), [large, small]) is small


def test_lane_pixel_points_are_scaled():
    lane = LaneRegion("lane", 1, "normal", "south", ((0.1, 0.2), (0.5, 0.2), (0.5, 0.8), (0.1, 0.8)))
    points = lane.pixel_points(200, 100)
    assert points[0] == (20, 20)
    assert points[2] == (100, 80)
    assert lane.contains_pixel((50, 50), 100, 100)
