"""Domain models shared by detection, lane tracking, annotation, and reporting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

AnchorType = tuple[float, float]


@dataclass
class TrackedObject:
    """A single vehicle observation for one frame.

    ``bbox`` is ``(x1, y1, x2, y2)`` in pixel coordinates.
    """

    track_id: int
    class_id: int
    class_name: str
    confidence: float
    bbox: tuple[float, float, float, float]

    @property
    def center(self) -> AnchorType:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    @property
    def width(self) -> float:
        return max(0.0, self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> float:
        return max(0.0, self.bbox[3] - self.bbox[1])

    def anchor(self, kind: str = "bottom_center") -> AnchorType:
        """Reference point used to decide which lane the vehicle occupies."""
        x1, y1, x2, y2 = self.bbox
        if kind == "center":
            return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        if kind == "bottom_left":
            return (x1, y2)
        if kind == "bottom_right":
            return (x2, y2)
        if kind == "top_center":
            return ((x1 + x2) / 2.0, y1)
        # Default and safe fallback: the tyre/road contact point.
        return ((x1 + x2) / 2.0, y2)

    def directional_anchor(
        self,
        *,
        frame_width: float,
        small_px: float,
        large_px: float,
        fallback: str = "bottom_center",
    ) -> AnchorType:
        """Bottom anchor that slides to the outer bottom corner for wide boxes.

        Near-field boxes are wide because of perspective and their bottom-centre can
        land on the wrong side of a painted divider. Boxes in the left half of the
        frame slide toward their bottom-left corner; boxes in the right half slide
        toward their bottom-right corner (the outer edge, away from the median).
        The slide is linear between ``small_px`` (no shift) and ``large_px`` (full
        shift to the corner).

        A non-positive ``frame_width`` falls back to ``anchor(fallback)``.
        """
        x1, y1, x2, y2 = self.bbox
        if frame_width <= 0:
            return self.anchor(fallback)
        center_x = (x1 + x2) / 2.0
        edge_x = x1 if center_x < frame_width / 2.0 else x2

        span = float(large_px) - float(small_px)
        width = self.width
        if span <= 0:
            weight = 1.0 if width >= float(large_px) else 0.0
        else:
            weight = (width - float(small_px)) / span
        weight = min(1.0, max(0.0, weight))

        return (center_x + (edge_x - center_x) * weight, y2)

    def as_int_bbox(self) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = self.bbox
        return int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))


@dataclass
class LaneStint:
    """One continuous period a vehicle spends in a lane."""

    stint_id: int
    lane_name: str
    lane_index: int | None
    lane_role: str
    enter_frame: int
    enter_time_s: float
    enter_timestamp: str
    exit_frame: int | None = None
    exit_time_s: float | None = None
    exit_timestamp: str = ""
    dwell_seconds: float = 0.0

    @property
    def open(self) -> bool:
        return self.exit_frame is None

    def close(self, *, frame_index: int, time_s: float, timestamp: str) -> float:
        self.exit_frame = int(frame_index)
        self.exit_time_s = round(float(time_s), 3)
        self.exit_timestamp = timestamp
        self.dwell_seconds = round(max(0.0, float(time_s) - self.enter_time_s), 3)
        return self.dwell_seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LaneEvent:
    """Structured event streamed to JSONL for the data pipeline.

    ``event_type`` is one of ``lane_enter``, ``lane_exit``, ``lane_change``,
    or ``lane_hogging_indication``.
    """

    camera_id: str
    timestamp: str
    track_id: int
    object_class: str
    confidence: float
    event_type: str
    lane_name: str
    lane_index: int | None = None
    lane_role: str = ""
    previous_lane: str = ""
    dwell_seconds: float = 0.0
    class_id: int = 0
    frame_index: int = 0
    video_time_s: float = 0.0
    severity: str = ""
    rule_name: str = ""
    message: str = ""
    schema_version: str = "1.0"
    session_id: str = ""
    window_index: int | None = None
    window_count: int | None = None
    event_id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrackSummary:
    """End-of-track summary, including every lane stint observed."""

    camera_id: str
    track_id: int
    object_class: str
    class_id: int
    first_frame: int
    last_frame: int
    first_timestamp: str
    last_timestamp: str
    first_video_time_s: float
    last_video_time_s: float
    lane_stints: list[LaneStint] = field(default_factory=list)
    lane_change_count: int = 0
    hogging_event_count: int = 0
    dominant_lane: str = ""
    total_lane_dwell_seconds: float = 0.0
    session_id: str = ""
    window_index: int | None = None
    window_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "track_id": self.track_id,
            "object_class": self.object_class,
            "class_id": self.class_id,
            "first_frame": self.first_frame,
            "last_frame": self.last_frame,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "first_video_time_s": self.first_video_time_s,
            "last_video_time_s": self.last_video_time_s,
            "lane_change_count": self.lane_change_count,
            "hogging_event_count": self.hogging_event_count,
            "dominant_lane": self.dominant_lane,
            "total_lane_dwell_seconds": self.total_lane_dwell_seconds,
            "session_id": self.session_id,
            "window_index": self.window_index,
            "window_count": self.window_count,
            "lane_stints": [stint.to_dict() for stint in self.lane_stints],
        }


EVENT_TYPES = (
    "lane_enter",
    "lane_exit",
    "lane_change",
    "lane_hogging_indication",
)
