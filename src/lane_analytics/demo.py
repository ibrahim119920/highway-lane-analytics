"""Offline synthetic multi-lane scene + manifest tracker.

The demo renders a top-down three-lane road with deterministic vehicles,
including some lane changes and heavy vehicles that remain in the overtaking
lane. A per-frame manifest of ground-truth boxes is replayed through the normal
pipeline, so lane occupancy, lane-change, and hogging logic can be exercised
without YOLO, PyTorch, or a video download.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

from .config import (
    AnalyticsConfig,
    AppConfig,
    HoggingConfig,
    HoggingRuleConfig,
    LaneConfig,
    OccupancyConfig,
    OutputConfig,
    VideoConfig,
)
from .models import TrackedObject
from .pipeline import RunResult, process_video
from .utils import ensure_dir

log = logging.getLogger(__name__)

REFERENCE_WIDTH = 640.0
REFERENCE_HEIGHT = 640.0
LANE_X = (0.24, 0.49, 0.75)
LANE_CHANGE_FADE_S = 1.2
BASE_TIMESTAMP = "2026-11-07T08:00:00"

CLASS_SPECS = {
    "car": {"class_id": 2, "size": (46, 88), "speed": 92.0},
    "truck": {"class_id": 7, "size": (58, 116), "speed": 66.0},
    "bus": {"class_id": 5, "size": (56, 122), "speed": 72.0},
}

CLASS_COLORS = {
    "car": [(60, 160, 230), (200, 200, 200), (70, 70, 200)],
    "truck": [(140, 110, 60), (60, 120, 170)],
    "bus": [(40, 160, 220), (30, 70, 200)],
}


@dataclass(frozen=True)
class DemoVehicle:
    track_id: int
    class_id: int
    class_name: str
    lane_plan: tuple[tuple[float, int], ...]
    spawn_y: float
    speed: float
    width: int
    height: int
    color: tuple[int, int, int]
    confidence: float


@dataclass
class DemoResult:
    run: RunResult
    input_video: str
    manifest_path: str
    preview_path: str | None
    vehicle_count: int


class ManifestDetector:
    """Replays pre-recorded detections; used by the offline demo only."""

    name = "synthetic-lane-manifest"

    def __init__(self, manifest_path: str | Path) -> None:
        self._frames: dict[int, list[TrackedObject]] = {}
        with Path(manifest_path).open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                record = json.loads(line)
                objects = [
                    TrackedObject(
                        track_id=int(item["track_id"]),
                        class_id=int(item["class_id"]),
                        class_name=str(item["class_name"]),
                        confidence=float(item["confidence"]),
                        bbox=tuple(float(value) for value in item["bbox"]),
                    )
                    for item in record.get("objects", [])
                ]
                self._frames[int(record["frame_index"])] = objects

    def update(self, frame: np.ndarray, frame_index: int) -> list[TrackedObject]:
        return self._frames.get(frame_index, [])


def _build_vehicles(width: int, height: int, seed: int) -> list[DemoVehicle]:
    rng = random.Random(seed)
    scale_x = width / REFERENCE_WIDTH
    scale_y = height / REFERENCE_HEIGHT
    specs = [
        # class, lane plan (start_seconds, lane_index), spawn y, speed multiplier
        ("car", ((0.0, 0),), -120.0, 1.00),
        ("car", ((0.0, 1), (5.0, 2)), -280.0, 0.94),
        ("truck", ((0.0, 2),), -40.0, 1.00),
        ("bus", ((0.0, 1),), -420.0, 1.00),
        ("car", ((0.0, 0), (9.0, 1)), -520.0, 1.04),
        ("car", ((0.0, 2),), -640.0, 0.88),
    ]
    vehicles: list[DemoVehicle] = []
    for track_id, (class_name, lane_plan, spawn_y, speed_multiplier) in enumerate(specs, start=1):
        spec = CLASS_SPECS[class_name]
        spec_width, spec_height = spec["size"]
        color = rng.choice(CLASS_COLORS[class_name])
        vehicles.append(
            DemoVehicle(
                track_id=track_id,
                class_id=int(spec["class_id"]),
                class_name=class_name,
                lane_plan=lane_plan,
                spawn_y=spawn_y * scale_y,
                speed=spec["speed"] * speed_multiplier * scale_y,
                width=max(10, int(round(spec_width * scale_x))),
                height=max(18, int(round(spec_height * scale_y))),
                color=color,
                confidence=round(rng.uniform(0.88, 0.98), 3),
            )
        )
    return vehicles


def _lane_center_x(lane_index: int, width: int) -> float:
    return LANE_X[lane_index] * width


def _vehicle_x(vehicle: DemoVehicle, time_s: float, width: int) -> float:
    """Piecewise lane-centre x position with a smooth transition."""
    plan = vehicle.lane_plan
    current_lane = plan[0][1]
    previous_lane = plan[0][1]
    for start_time, lane_index in plan:
        if time_s + 1e-9 < start_time:
            break
        previous_lane = current_lane
        current_lane = lane_index
        change_start = start_time
    if current_lane == previous_lane:
        return _lane_center_x(current_lane, width)
    progress = min(1.0, max(0.0, (time_s - change_start) / LANE_CHANGE_FADE_S))
    start_x = _lane_center_x(previous_lane, width)
    end_x = _lane_center_x(current_lane, width)
    return start_x + (end_x - start_x) * progress


def _vehicle_bbox(
    vehicle: DemoVehicle, time_s: float, width: int, height: int
) -> tuple[float, float, float, float]:
    center_x = _vehicle_x(vehicle, time_s, width)
    center_y = vehicle.spawn_y + vehicle.speed * time_s
    return (
        center_x - vehicle.width / 2.0,
        center_y - vehicle.height / 2.0,
        center_x + vehicle.width / 2.0,
        center_y + vehicle.height / 2.0,
    )


def _draw_vehicle(
    image: np.ndarray,
    vehicle: DemoVehicle,
    bbox: tuple[float, float, float, float],
) -> None:
    x1, y1, x2, y2 = (int(round(value)) for value in bbox)
    if x2 - x1 < 4 or y2 - y1 < 6:
        return
    radius = max(3, int(min(x2 - x1, y2 - y1) * 0.18))

    cv2.rectangle(image, (x1 + 3, y1 + 4), (x2 + 3, y2 + 4), (28, 28, 30), -1)
    _rounded_rect(image, (x1, y1), (x2, y2), radius, vehicle.color, -1)
    cv2.rectangle(image, (x1, y1), (x2, y2), (24, 24, 26), 1)

    inset = max(3, int((x2 - x1) * 0.16))
    window_y1 = y1 + int((y2 - y1) * 0.16)
    window_y2 = y1 + int((y2 - y1) * 0.30)
    cv2.rectangle(image, (x1 + inset, window_y1), (x2 - inset, window_y2), (88, 108, 120), -1)
    if vehicle.class_name in {"truck", "bus"}:
        cargo_y1 = y1 + int((y2 - y1) * 0.38)
        cargo_y2 = y2 - int((y2 - y1) * 0.10)
        cargo_color = (205, 205, 205) if vehicle.class_name == "truck" else (95, 120, 135)
        cv2.rectangle(image, (x1 + inset, cargo_y1), (x2 - inset, cargo_y2), cargo_color, -1)

    light_radius = max(2, int((x2 - x1) * 0.07))
    for light_x in (x1 + int((x2 - x1) * 0.20), x2 - int((x2 - x1) * 0.20)):
        cv2.circle(image, (light_x, y2 - int((y2 - y1) * 0.06)), light_radius, (225, 245, 255), -1)


def _rounded_rect(
    image: np.ndarray,
    top_left: tuple[int, int],
    bottom_right: tuple[int, int],
    radius: int,
    color: tuple[int, int, int],
    thickness: int = -1,
) -> None:
    x1, y1 = top_left
    x2, y2 = bottom_right
    radius = max(0, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    if thickness < 0:
        cv2.rectangle(image, (x1 + radius, y1), (x2 - radius, y2), color, -1)
        cv2.rectangle(image, (x1, y1 + radius), (x2, y2 - radius), color, -1)
        for center in (
            (x1 + radius, y1 + radius),
            (x2 - radius, y1 + radius),
            (x1 + radius, y2 - radius),
            (x2 - radius, y2 - radius),
        ):
            cv2.circle(image, center, radius, color, -1)
    else:
        cv2.rectangle(image, (x1 + radius, y1), (x2 - radius, y2), color, thickness)
        cv2.rectangle(image, (x1, y1 + radius), (x2, y2 - radius), color, thickness)


def _build_background(width: int, height: int) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :] = (40, 72, 48)

    left = int(width * 0.12)
    right = int(width * 0.88)
    sidewalk = max(10, int(width * 0.018))
    cv2.rectangle(image, (left - sidewalk, 0), (left, height), (108, 108, 114), -1)
    cv2.rectangle(image, (right, 0), (right + sidewalk, height), (108, 108, 114), -1)
    cv2.rectangle(image, (left, 0), (right, height), (56, 56, 60), -1)

    lane_width = (right - left) / 3.0
    cv2.line(image, (left, 0), (left, height), (230, 230, 230), 4)
    cv2.line(image, (right, 0), (right, height), (230, 230, 230), 4)
    for divider in (1, 2):
        x = int(left + lane_width * divider)
        for dash_y in range(0, height, 70):
            cv2.line(image, (x, dash_y), (x, min(dash_y + 38, height)), (230, 230, 230), 4)

    for lane_index, center in enumerate(LANE_X):
        x = int(center * width)
        cv2.arrowedLine(
            image,
            (x, int(height * 0.18)),
            (x, int(height * 0.30)),
            (205, 205, 205),
            3,
            cv2.LINE_AA,
            tipLength=0.35,
        )
        label = "OVERTAKING" if lane_index == 2 else f"LANE {lane_index + 1}"
        cv2.putText(
            image,
            label,
            (x - 40, int(height * 0.10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.40,
            (190, 190, 190),
            1,
            cv2.LINE_AA,
        )

    cv2.putText(
        image,
        "SYNTHETIC LANE SCENE",
        (12, height - 14),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.50,
        (170, 180, 170),
        1,
        cv2.LINE_AA,
    )
    return image


def _render_video(
    video_path: Path,
    manifest_path: Path,
    vehicles: Sequence[DemoVehicle],
    *,
    width: int,
    height: int,
    fps: float,
    duration_s: float,
) -> int:
    background = _build_background(width, height)
    writer = cv2.VideoWriter(
        str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    if not writer.isOpened():
        raise RuntimeError(f"Could not create demo video writer: {video_path}")

    frame_count = int(round(duration_s * fps))
    try:
        with manifest_path.open("w", encoding="utf-8") as manifest:
            for frame_index in range(frame_count):
                time_s = frame_index / fps
                frame = background.copy()
                objects = []
                for vehicle in vehicles:
                    bbox = _vehicle_bbox(vehicle, time_s, width, height)
                    if bbox[3] < -24 or bbox[1] > height + 24:
                        continue
                    if bbox[2] < 0 or bbox[0] > width:
                        continue
                    _draw_vehicle(frame, vehicle, bbox)
                    if bbox[3] > 0 and bbox[1] < height:
                        objects.append(
                            {
                                "track_id": vehicle.track_id,
                                "class_id": vehicle.class_id,
                                "class_name": vehicle.class_name,
                                "confidence": vehicle.confidence,
                                "bbox": [round(value, 2) for value in bbox],
                            }
                        )
                manifest.write(json.dumps({"frame_index": frame_index, "objects": objects}) + "\n")
                writer.write(frame)
    finally:
        writer.release()
    return frame_count


def _extract_preview(video_path: Path, frame_index: int, output_path: Path) -> str | None:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return None
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_index))
        ok, frame = capture.read()
        if not ok or frame is None:
            return None
        cv2.imwrite(str(output_path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        return str(output_path)
    finally:
        capture.release()


def _demo_lanes() -> list[LaneConfig]:
    return [
        LaneConfig(
            name="lane_left",
            index=1,
            direction="southbound",
            role="normal",
            polygon=[(0.12, 0.03), (0.36, 0.03), (0.36, 0.97), (0.12, 0.97)],
        ),
        LaneConfig(
            name="lane_center",
            index=2,
            direction="southbound",
            role="normal",
            polygon=[(0.365, 0.03), (0.615, 0.03), (0.615, 0.97), (0.365, 0.97)],
        ),
        LaneConfig(
            name="lane_right",
            index=3,
            direction="southbound",
            role="overtaking",
            polygon=[(0.62, 0.03), (0.88, 0.03), (0.88, 0.97), (0.62, 0.97)],
        ),
    ]


def run_demo(
    output_dir: str | Path = "output/demo",
    *,
    duration: float = 20.0,
    fps: int = 10,
    width: int = 640,
    height: int = 640,
    seed: int = 7,
    frame_stride: int = 1,
    preview_frame: int = 120,
) -> DemoResult:
    """Generate the synthetic scene, run the full pipeline, and extract a preview."""
    if duration <= 0 or fps <= 0:
        raise ValueError("duration and fps must be positive")
    width = max(320, int(width))
    height = max(240, int(height))

    target = ensure_dir(Path(output_dir))
    input_dir = ensure_dir(target / "input")
    input_video = input_dir / "demo_input.mp4"
    manifest_path = input_dir / "demo_input.manifest.jsonl"

    log.info("Generating synthetic lane scene: %dx%d, %.1fs @ %d fps", width, height, duration, fps)
    vehicles = _build_vehicles(width, height, seed)
    frames = _render_video(
        input_video,
        manifest_path,
        vehicles,
        width=width,
        height=height,
        fps=float(fps),
        duration_s=duration,
    )
    log.info("Wrote %s (%d frames, %d vehicles)", input_video, frames, len(vehicles))

    config = AppConfig(
        camera_id="demo_camera_01",
        base_timestamp=BASE_TIMESTAMP,
        video=VideoConfig(path=str(input_video), frame_stride=frame_stride),
        lanes=_demo_lanes(),
        occupancy=OccupancyConfig(
            anchor="bottom_center",
            min_confidence=0.3,
            min_lane_frames=2,
            max_track_age_frames=20,
        ),
        hogging=HoggingConfig(
            enabled=True,
            retrigger_after_seconds=0.0,
            rules=[
                HoggingRuleConfig(
                    name="heavy_vehicle_in_overtaking_lane",
                    lane_roles=["overtaking"],
                    classes=["truck", "bus"],
                    min_dwell_seconds=2.0,
                    severity="high",
                    description="Truk/bus menggunakan lajur overtaking melebihi batas waktu.",
                ),
                HoggingRuleConfig(
                    name="prolonged_overtaking_lane",
                    lane_roles=["overtaking"],
                    classes=["car"],
                    min_dwell_seconds=6.0,
                    severity="medium",
                    description="Mobil bertahan di lajur overtaking melebihi batas waktu.",
                ),
            ],
        ),
        analytics=AnalyticsConfig(interval_seconds=5),
        output=OutputConfig(dir=str(target)),
    )
    config.validate()

    annotated_video = target / "demo_annotated.mp4"
    events_path = target / "demo_events.jsonl"
    tracks_path = target / "demo_tracks.jsonl"
    reports_dir = target / "reports"

    run = process_video(
        config,
        detector=ManifestDetector(manifest_path),
        output_video=annotated_video,
        events_path=events_path,
        tracks_path=tracks_path,
        reports_dir=reports_dir,
        run_id="demo_run",
    )

    preview_path = None
    if preview_frame >= 0:
        preview_path = _extract_preview(
            annotated_video,
            preview_frame // max(1, frame_stride),
            target / "preview.jpg",
        )

    return DemoResult(
        run=run,
        input_video=str(input_video),
        manifest_path=str(manifest_path),
        preview_path=preview_path,
        vehicle_count=len(vehicles),
    )
