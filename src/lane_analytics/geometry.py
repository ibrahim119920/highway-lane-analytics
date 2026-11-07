"""Polygon geometry for lane regions and anchor-point lane assignment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

Point = tuple[float, float]

_EPS = 1e-9


def _on_segment(point: Point, first: Point, second: Point, eps: float = 1e-9) -> bool:
    """True when ``point`` lies on the segment ``first -> second``."""
    px, py = point
    x1, y1 = first
    x2, y2 = second
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    if abs(cross) > eps:
        return False
    dot = (px - x1) * (px - x2) + (py - y1) * (py - y2)
    return dot <= eps


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray-casting point-in-polygon test, including boundary points.

    The polygon is treated as implicitly closed. Points exactly on an edge are
    considered inside so that a vehicle straddling a painted lane divider is
    still assigned to a lane instead of becoming unassigned for one frame.
    """
    if len(polygon) < 3:
        return False

    x, y = point
    inside = False
    count = len(polygon)
    for index in range(count):
        first = polygon[index]
        second = polygon[(index + 1) % count]
        if _on_segment(point, first, second):
            return True
        x1, y1 = first
        x2, y2 = second
        if (y1 > y) != (y2 > y):
            x_intersection = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_intersection:
                inside = not inside
    return inside


def polygon_area(polygon: Sequence[Point]) -> float:
    """Absolute shoelace area of an implicitly closed polygon."""
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polygon_centroid(polygon: Sequence[Point]) -> Point:
    """Centroid of a polygon; falls back to the vertex average when degenerate."""
    area = polygon_area(polygon)
    if area <= _EPS:
        count = max(1, len(polygon))
        return (
            sum(point[0] for point in polygon) / count,
            sum(point[1] for point in polygon) / count,
        )
    cx = cy = 0.0
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    factor = 1.0 / (6.0 * area)
    return (cx * factor, cy * factor)


@dataclass(frozen=True)
class LaneRegion:
    """A configured lane polygon in normalized coordinates."""

    name: str
    index: int | None
    role: str
    direction: str
    points: tuple[Point, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 3:
            raise ValueError(f"Lane '{self.name}' requires at least 3 polygon points")
        if polygon_area(self.points) <= _EPS:
            raise ValueError(f"Lane '{self.name}' has zero area")

    @property
    def area(self) -> float:
        return polygon_area(self.points)

    @property
    def centroid(self) -> Point:
        return polygon_centroid(self.points)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "index": self.index,
            "role": self.role,
            "direction": self.direction,
            "polygon": [list(point) for point in self.points],
        }

    def pixel_points(self, width: int, height: int) -> list[tuple[int, int]]:
        return [
            (int(round(point[0] * width)), int(round(point[1] * height)))
            for point in self.points
        ]

    def contains(self, point: Point) -> bool:
        """Test a normalized point."""
        return point_in_polygon(point, self.points)

    def contains_pixel(self, point: Point, width: int, height: int) -> bool:
        if width <= 0 or height <= 0:
            return False
        return self.contains((point[0] / width, point[1] / height))


def assign_lane(point: Point, lanes: Iterable[LaneRegion]) -> LaneRegion | None:
    """Return the lane containing a normalized point.

    Overlapping polygons are allowed (for example, a shoulder overlapping a
    lane). When more than one contains the point, the smallest area wins, which
    makes the assignment stable for nested or overlapping calibration errors.
    """
    matches = [lane for lane in lanes if lane.contains(point)]
    if not matches:
        return None
    return min(matches, key=lambda lane: (lane.area, lane.index if lane.index is not None else 0))
