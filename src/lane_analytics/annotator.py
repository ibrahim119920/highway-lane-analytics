"""OpenCV visualization: lane polygons, vehicle status, and live HUD."""

from __future__ import annotations

from collections import deque
from typing import Iterable, Sequence

import cv2
import numpy as np

from .geometry import LaneRegion
from .models import TrackedObject

CLASS_COLORS: dict[str, tuple[int, int, int]] = {
    "car": (0, 200, 255),
    "truck": (0, 220, 130),
    "bus": (0, 90, 255),
}
DEFAULT_COLOR = (200, 200, 200)
HOGGING_COLOR = (40, 40, 230)
LANE_COLORS: dict[str, tuple[int, int, int]] = {
    "normal": (110, 190, 120),
    "overtaking": (60, 170, 255),
    "restricted": (80, 80, 220),
}
DEFAULT_LANE_COLOR = (120, 160, 220)


def class_color(name: str) -> tuple[int, int, int]:
    return CLASS_COLORS.get(name, DEFAULT_COLOR)


def lane_color(role: str) -> tuple[int, int, int]:
    return LANE_COLORS.get(role, DEFAULT_LANE_COLOR)


class Annotator:
    """Stateful frame renderer (keeps short motion trails per track)."""

    def __init__(
        self,
        lanes: Sequence[LaneRegion],
        camera_id: str,
        *,
        trail_length: int = 16,
        show_trails: bool = True,
        backend_label: str = "YOLO + ByteTrack",
    ) -> None:
        self._lanes = list(lanes)
        self._camera_id = camera_id
        self._backend_label = backend_label
        self._trail_length = max(0, trail_length)
        self._show_trails = show_trails and trail_length > 0
        self._trails: dict[int, deque[tuple[int, int]]] = {}
        self._last_seen: dict[int, int] = {}
        self._track_class: dict[int, str] = {}

    def draw(
        self,
        frame: np.ndarray,
        objects: Iterable[TrackedObject],
        *,
        frame_index: int,
        timestamp: str,
        status_by_track: dict[int, dict[str, object]],
        counters: dict[str, object],
        processing_fps: float | None = None,
    ) -> np.ndarray:
        height, width = frame.shape[:2]
        scale = max(0.45, min(0.75, width / 1600.0))

        self.draw_lanes(frame)

        objects = list(objects)
        if self._show_trails:
            for obj in objects:
                center = (int(obj.center[0]), int(obj.center[1]))
                trail = self._trails.setdefault(obj.track_id, deque(maxlen=self._trail_length))
                trail.append(center)
                self._last_seen[obj.track_id] = frame_index
                self._track_class[obj.track_id] = obj.class_name
            self._prune_trails(frame_index)
            self._draw_trails(frame)

        # Two passes: every box first, then every label. A label can never be
        # drawn underneath another object's bounding box.
        for obj in objects:
            self._draw_box(frame, obj, status_by_track.get(obj.track_id, {}))
        for obj in objects:
            self._draw_label(frame, obj, status_by_track.get(obj.track_id, {}), scale)

        self._draw_hud(
            frame,
            frame_index=frame_index,
            timestamp=timestamp,
            counters=counters,
            processing_fps=processing_fps,
            scale=scale,
        )
        return frame

    # -------------------------------------------------------------------- lanes
    def draw_lanes(self, frame: np.ndarray) -> np.ndarray:
        """Draw configured lane polygons (also used by the preview tool)."""
        height, width = frame.shape[:2]
        overlay = frame.copy()
        for lane in self._lanes:
            points = lane.pixel_points(width, height)
            color = lane_color(lane.role)
            cv2.fillPoly(overlay, [np.array(points, dtype=np.int32)], color)
        cv2.addWeighted(overlay, 0.12, frame, 0.88, 0, dst=frame)

        placements: list[
            tuple[tuple[int, int, int], str, int, int, int, int, str]
        ] = []
        for lane in self._lanes:
            points = lane.pixel_points(width, height)
            color = lane_color(lane.role)
            cv2.polylines(
                frame,
                [np.array(points, dtype=np.int32)],
                True,
                color,
                2,
                cv2.LINE_AA,
            )
            anchor_x, anchor_y = self._lane_label_anchor(points)
            label = f"{lane.name}"
            if lane.index is not None:
                label = f"L{lane.index} {label}"
            if lane.role:
                label += f" [{lane.role}]"
            label_x, label_y, align = self._lane_label_position(
                lane, anchor_x, anchor_y, height
            )
            placements.append(
                (color, label, anchor_x, anchor_y, label_x, label_y, align)
            )

        # Leader lines first, labels second: a line can never cross the label text.
        for color, _, anchor_x, anchor_y, label_x, label_y, _ in placements:
            cv2.line(
                frame,
                (anchor_x, anchor_y),
                (label_x, label_y + 2),
                color,
                1,
                cv2.LINE_AA,
            )
        for color, label, _, _, label_x, label_y, align in placements:
            self._label(frame, label, (label_x, label_y), color, align=align)
        return frame

    @staticmethod
    def _lane_label_anchor(points: Sequence[tuple[int, int]]) -> tuple[int, int]:
        """Top edge midpoint of a lane polygon, used as its label anchor."""
        top_y = min(point[1] for point in points)
        top_xs = [point[0] for point in points if abs(point[1] - top_y) <= 1]
        return int(round(sum(top_xs) / len(top_xs))), int(top_y)

    @staticmethod
    def _lane_label_position(
        lane: LaneRegion,
        anchor_x: int,
        anchor_y: int,
        frame_height: int,
    ) -> tuple[int, int, str]:
        """Place a lane label high in the sky, clear of the horizon and traffic.

        The label is lifted by a fraction of the frame height and staggered per
        lane index. Northbound labels grow leftward and southbound labels grow
        rightward, so both directions stay clear of the median.
        """
        index = max(1, lane.index or 1)
        base_gap = int(round(frame_height * 0.14))
        stagger = int(round(frame_height * 0.045)) * (index - 1)
        label_y = max(30, anchor_y - base_gap - stagger)
        if str(lane.direction).lower().startswith("north"):
            return anchor_x - 4, label_y, "right"
        return anchor_x + 4, label_y, "left"

    # ------------------------------------------------------------------- trails
    def _draw_trails(self, frame: np.ndarray) -> None:
        overlay = frame.copy()
        for track_id, points in self._trails.items():
            if len(points) < 2:
                continue
            color = class_color(self._track_class.get(track_id, ""))
            cv2.polylines(overlay, [np.array(points, dtype=np.int32)], False, color, 2, cv2.LINE_AA)
        cv2.addWeighted(overlay, 0.30, frame, 0.70, 0, dst=frame)

    def _prune_trails(self, frame_index: int) -> None:
        stale = [
            track_id
            for track_id in self._trails
            if frame_index - self._last_seen.get(track_id, 0) > 90
        ]
        for track_id in stale:
            self._trails.pop(track_id, None)
            self._last_seen.pop(track_id, None)
            self._track_class.pop(track_id, None)

    # ------------------------------------------------------------------ objects
    def _draw_box(
        self,
        frame: np.ndarray,
        obj: TrackedObject,
        status: dict[str, object],
    ) -> None:
        height, width = frame.shape[:2]
        hogging = bool(status.get("hogging"))
        color = HOGGING_COLOR if hogging else class_color(obj.class_name)
        x1, y1, x2, y2 = obj.as_int_bbox()
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width - 1, x2), min(height - 1, y2)
        if x2 <= x1 or y2 <= y1:
            return

        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
        corner = max(6, int(min(x2 - x1, y2 - y1) * 0.18))
        for start, end in (
            ((x1, y1), (x1 + corner, y1)),
            ((x1, y1), (x1, y1 + corner)),
            ((x2, y1), (x2 - corner, y1)),
            ((x2, y1), (x2, y1 + corner)),
            ((x1, y2), (x1 + corner, y2)),
            ((x1, y2), (x1, y2 - corner)),
            ((x2, y2), (x2 - corner, y2)),
            ((x2, y2), (x2, y2 - corner)),
        ):
            cv2.line(frame, start, end, color, 3, cv2.LINE_AA)

        # Lane-assignment anchor (bottom-centre, or shifted for wide boxes).
        anchor = status.get("anchor")
        if isinstance(anchor, (tuple, list)) and len(anchor) == 2:
            anchor_x = int(round(float(anchor[0])))
            anchor_y = int(round(float(anchor[1])))
            cv2.circle(frame, (anchor_x, anchor_y), 4, (255, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(frame, (anchor_x, anchor_y), 6, color, 1, cv2.LINE_AA)

    def _draw_label(
        self,
        frame: np.ndarray,
        obj: TrackedObject,
        status: dict[str, object],
        scale: float,
    ) -> None:
        height, width = frame.shape[:2]
        hogging = bool(status.get("hogging"))
        color = HOGGING_COLOR if hogging else class_color(obj.class_name)
        x1, y1, x2, y2 = obj.as_int_bbox()
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width - 1, x2), min(height - 1, y2)
        if x2 <= x1 or y2 <= y1:
            return

        lane_name = str(status.get("lane") or "")
        dwell = float(status.get("dwell_seconds") or 0.0)
        lane_text = f"{lane_name} {dwell:.1f}s" if lane_name else "lane n/a"
        label = f"{obj.class_name} #{obj.track_id} {lane_text}"
        font_scale = max(0.42, 0.50 * (scale / 0.65))
        thickness_text = 1
        (text_w, text_h), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness_text
        )
        label_y1 = y1 - text_h - baseline - 7
        if label_y1 < 0:
            label_y1 = y1 + 2
        label_x2 = min(width - 1, x1 + text_w + 10)
        # Translucent background: boxes and the vehicle remain visible underneath.
        self._fill_rect_alpha(
            frame,
            (x1, label_y1),
            (label_x2, label_y1 + text_h + baseline + 4),
            color,
            0.78,
        )
        text_color = (15, 15, 15) if sum(color) > 360 else (245, 245, 245)
        cv2.putText(
            frame,
            label,
            (x1 + 5, label_y1 + text_h + 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            text_color,
            thickness_text,
            cv2.LINE_AA,
        )

        if hogging:
            rule = str(status.get("rule") or "lane_hogging")
            severity = str(status.get("severity") or "")
            tag = f"! HOGGING {severity}".strip() if severity else "! HOGGING"
            tag_y = label_y1 + text_h + baseline + 8
            (tag_w, tag_h), tag_base = cv2.getTextSize(
                tag, cv2.FONT_HERSHEY_SIMPLEX, font_scale * 0.9, 1
            )
            self._fill_rect_alpha(
                frame,
                (x1, tag_y),
                (
                    min(width - 1, x1 + tag_w + 8),
                    min(height - 1, tag_y + tag_h + tag_base + 4),
                ),
                HOGGING_COLOR,
                0.88,
            )
            cv2.putText(
                frame,
                tag,
                (x1 + 4, tag_y + tag_h + 1),
                cv2.FONT_HERSHEY_SIMPLEX,
                font_scale * 0.9,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )
            cv2.putText(
                frame,
                rule,
                (x1 + tag_w + 12, tag_y + tag_h + 1),
                cv2.FONT_HERSHEY_SIMPLEX,
                font_scale * 0.75,
                (210, 210, 210),
                1,
                cv2.LINE_AA,
            )

    @staticmethod
    def _fill_rect_alpha(
        frame: np.ndarray,
        top_left: tuple[int, int],
        bottom_right: tuple[int, int],
        color: tuple[int, int, int],
        alpha: float,
    ) -> None:
        """Blend a filled rectangle with the pixels already in ``frame``."""
        height, width = frame.shape[:2]
        x1, y1 = max(0, top_left[0]), max(0, top_left[1])
        x2, y2 = min(width - 1, bottom_right[0]), min(height - 1, bottom_right[1])
        if x2 <= x1 or y2 <= y1:
            return
        roi = frame[y1:y2, x1:x2]
        overlay = np.empty_like(roi)
        overlay[:] = color
        cv2.addWeighted(overlay, float(alpha), roi, 1.0 - float(alpha), 0, dst=roi)

    # --------------------------------------------------------------------- HUD
    def _draw_hud(
        self,
        frame: np.ndarray,
        *,
        frame_index: int,
        timestamp: str,
        counters: dict[str, object],
        processing_fps: float | None,
        scale: float,
    ) -> None:
        height, width = frame.shape[:2]
        by_class = dict(counters.get("by_class") or {})
        by_lane = dict(counters.get("by_lane") or {})
        active = int(counters.get("active_tracks") or 0)
        tracks_seen = int(counters.get("tracks_seen") or 0)
        lane_changes = int(counters.get("lane_changes") or 0)
        hogging = int(counters.get("hogging_indications") or 0)

        rows = sorted(by_lane.items(), key=lambda item: (-item[1], item[0]))[:6]
        panel_w = int(min(330, max(220, width * 0.28)))
        panel_h = int(104 + 21 * max(1, len(rows)))
        overlay = frame.copy()
        cv2.rectangle(overlay, (12, 12), (12 + panel_w, 12 + panel_h), (18, 18, 18), -1)
        cv2.addWeighted(overlay, 0.66, frame, 0.34, 0, dst=frame)

        cv2.putText(
            frame,
            "LANE OCCUPANCY",
            (24, 36),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (0, 220, 255),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            self._camera_id,
            (24, 58),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            (215, 215, 215),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            timestamp.replace("T", " "),
            (24, 78),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.41,
            (170, 170, 170),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            f"active {active}   seen {tracks_seen}",
            (24, 98),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.40,
            (190, 190, 190),
            1,
            cv2.LINE_AA,
        )

        y = 122
        for lane_name, count in rows:
            cv2.circle(frame, (29, y - 5), 5, CLASS_COLORS["car"], -1, cv2.LINE_AA)
            cv2.putText(
                frame,
                f"{lane_name:<16}",
                (40, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.40,
                (225, 225, 225),
                1,
                cv2.LINE_AA,
            )
            cv2.putText(
                frame,
                str(count),
                (40 + int(panel_w * 0.60), y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.40,
                (225, 225, 225),
                1,
                cv2.LINE_AA,
            )
            y += 21

        # Top-right panel: lane changes and hogging indications.
        total_w = int(min(220, width * 0.22))
        total_panel_h = 126
        overlay = frame.copy()
        cv2.rectangle(
            overlay,
            (width - total_w - 12, 12),
            (width - 12, 12 + total_panel_h),
            (18, 18, 18),
            -1,
        )
        cv2.addWeighted(overlay, 0.66, frame, 0.34, 0, dst=frame)
        cv2.putText(
            frame,
            "BEHAVIOUR",
            (width - total_w, 34),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (170, 170, 170),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            f"{lane_changes:04d}",
            (width - total_w, 72),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.25 if width >= 1000 else 0.9,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            "lane changes",
            (width - total_w, 92),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            f"{hogging:04d}",
            (width - total_w, 120),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.25 if width >= 1000 else 0.9,
            HOGGING_COLOR,
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            "hogging indications",
            (width - total_w, 138),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            (200, 200, 200),
            1,
            cv2.LINE_AA,
        )

        # Bottom status bar.
        bar_h = 30
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, height - bar_h), (width, height), (12, 12, 12), -1)
        cv2.addWeighted(overlay, 0.62, frame, 0.38, 0, dst=frame)
        fps_text = f"{processing_fps:.1f} fps" if processing_fps else "fps n/a"
        status = (
            f"frame {frame_index:06d}   |   {fps_text}   |   "
            f"{self._backend_label}   |   lane polygons + ByteTrack"
        )
        cv2.putText(
            frame,
            status,
            (14, height - 9),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.40,
            (205, 205, 205),
            1,
            cv2.LINE_AA,
        )

    @staticmethod
    def _label(
        frame: np.ndarray,
        text: str,
        position: tuple[int, int],
        color: tuple[int, int, int],
        *,
        align: str = "left",
    ) -> None:
        font_scale = 0.42
        (text_w, text_h), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)
        x, y = position
        if align == "right":
            x -= text_w
        elif align == "center":
            x -= text_w // 2
        x = max(2, min(frame.shape[1] - text_w - 4, x))
        y = max(text_h + 2, min(frame.shape[0] - baseline - 3, y))
        cv2.rectangle(
            frame,
            (x - 3, y - text_h - baseline - 2),
            (x + text_w + 3, y + 2),
            (20, 20, 20),
            -1,
        )
        cv2.putText(
            frame,
            text,
            (x, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            color,
            1,
            cv2.LINE_AA,
        )
