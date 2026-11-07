"""Typed YAML configuration with validation and clear error messages."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .geometry import polygon_area
from .utils import parse_timestamp

ANCHOR_TYPES = ("center", "bottom_center", "bottom_left", "bottom_right", "top_center")
SEVERITIES = ("low", "medium", "high")


class ConfigError(ValueError):
    """Raised when a configuration file is missing, malformed, or invalid."""


@dataclass
class ModelConfig:
    weights: str = "yolo11n.pt"
    device: str = "cpu"
    imgsz: int = 640
    conf: float = 0.35
    iou: float = 0.5
    tracker: str = "bytetrack.yaml"
    classes: list[int] = field(default_factory=lambda: [2, 5, 7])


@dataclass
class VideoConfig:
    path: str = "data/raw/traffic.mp4"
    frame_stride: int = 2
    max_frames: int | None = None
    output_fps: float | None = None


@dataclass
class LaneConfig:
    name: str = "lane_1"
    index: int | None = None
    direction: str = ""
    role: str = "normal"
    polygon: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class OccupancyConfig:
    anchor: str = "bottom_center"
    anchor_shift_enabled: bool = False
    anchor_shift_small_bbox_px: float = 90.0
    anchor_shift_large_bbox_px: float = 250.0
    direction_deadband_px: float = 2.0
    direction_min_votes: int = 2
    min_confidence: float = 0.3
    min_lane_frames: int = 2
    max_track_age_frames: int = 90
    emit_lane_enter: bool = True
    emit_lane_exit: bool = True
    emit_lane_change: bool = True


@dataclass
class HoggingRuleConfig:
    name: str = "rule"
    lanes: list[str] = field(default_factory=list)
    lane_roles: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    min_dwell_seconds: float = 8.0
    severity: str = "medium"
    description: str = ""


@dataclass
class HoggingConfig:
    enabled: bool = True
    retrigger_after_seconds: float = 0.0
    rules: list[HoggingRuleConfig] = field(default_factory=list)


@dataclass
class AnalyticsConfig:
    interval_seconds: int = 60


@dataclass
class OutputConfig:
    dir: str = "output"
    video: str | None = None
    events: str | None = None
    tracks: str | None = None
    reports_dir: str | None = None


@dataclass
class AppConfig:
    camera_id: str = "camera_01"
    base_timestamp: str = ""
    model: ModelConfig = field(default_factory=ModelConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    lanes: list[LaneConfig] = field(default_factory=list)
    occupancy: OccupancyConfig = field(default_factory=OccupancyConfig)
    hogging: HoggingConfig = field(default_factory=HoggingConfig)
    analytics: AnalyticsConfig = field(default_factory=AnalyticsConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    def validate(self) -> None:
        if not str(self.camera_id).strip():
            raise ConfigError("camera_id must not be empty")
        if self.base_timestamp:
            try:
                parse_timestamp(self.base_timestamp)
            except ValueError as exc:
                raise ConfigError(f"base_timestamp: {exc}") from exc

        model = self.model
        if not model.weights:
            raise ConfigError("model.weights must not be empty")
        if model.imgsz <= 0:
            raise ConfigError("model.imgsz must be a positive integer")
        if not 0.0 <= model.conf <= 1.0:
            raise ConfigError("model.conf must be between 0 and 1")
        if not 0.0 <= model.iou <= 1.0:
            raise ConfigError("model.iou must be between 0 and 1")
        if not model.tracker:
            raise ConfigError("model.tracker must not be empty")
        if not model.classes:
            raise ConfigError("model.classes must contain at least one COCO class id")
        if any(not isinstance(class_id, int) or class_id < 0 for class_id in model.classes):
            raise ConfigError("model.classes must be a list of non-negative integers")

        video = self.video
        if not video.path:
            raise ConfigError("video.path must not be empty")
        if video.frame_stride < 1:
            raise ConfigError("video.frame_stride must be >= 1")
        if video.max_frames is not None and video.max_frames < 1:
            raise ConfigError("video.max_frames must be null or >= 1")
        if video.output_fps is not None and video.output_fps <= 0:
            raise ConfigError("video.output_fps must be null or > 0")

        if not self.lanes:
            raise ConfigError("At least one lane polygon is required in 'lanes'")
        seen_names: set[str] = set()
        seen_indices: set[tuple[str, int]] = set()
        for position, lane in enumerate(self.lanes):
            label = f"lanes[{position}]"
            if not str(lane.name).strip():
                raise ConfigError(f"{label}.name must not be empty")
            if lane.name in seen_names:
                raise ConfigError(f"{label}.name '{lane.name}' is duplicated")
            seen_names.add(lane.name)
            if lane.index is not None:
                if not isinstance(lane.index, int) or lane.index < 1:
                    raise ConfigError(f"{label}.index must be a positive integer or null")
                # Lane indices are scoped per direction: both carriageways have a lane 1.
                index_key = (str(lane.direction), int(lane.index))
                if index_key in seen_indices:
                    raise ConfigError(
                        f"{label}.index {lane.index} is duplicated for "
                        f"direction '{lane.direction}'"
                    )
                seen_indices.add(index_key)
            if not str(lane.role).strip():
                raise ConfigError(f"{label}.role must not be empty")
            if len(lane.polygon) < 3:
                raise ConfigError(f"{label}.polygon requires at least 3 points")
            for point in lane.polygon:
                if len(point) != 2:
                    raise ConfigError(f"{label}.polygon points must contain exactly two values")
                if not all(0.0 <= float(value) <= 1.0 for value in point):
                    raise ConfigError(f"{label}.polygon coordinates must be normalized (0..1)")
            if polygon_area(lane.polygon) <= 1e-9:
                raise ConfigError(f"{label}.polygon has zero area")

        occupancy = self.occupancy
        if occupancy.anchor not in ANCHOR_TYPES:
            raise ConfigError(
                "occupancy.anchor must be one of: " + ", ".join(ANCHOR_TYPES)
            )
        if not 0.0 <= occupancy.min_confidence <= 1.0:
            raise ConfigError("occupancy.min_confidence must be between 0 and 1")
        if occupancy.min_lane_frames < 1:
            raise ConfigError("occupancy.min_lane_frames must be >= 1")
        if occupancy.anchor_shift_small_bbox_px < 0:
            raise ConfigError("occupancy.anchor_shift_small_bbox_px must be >= 0")
        if occupancy.anchor_shift_large_bbox_px <= occupancy.anchor_shift_small_bbox_px:
            raise ConfigError(
                "occupancy.anchor_shift_large_bbox_px must be greater than "
                "anchor_shift_small_bbox_px"
            )
        if occupancy.direction_deadband_px < 0:
            raise ConfigError("occupancy.direction_deadband_px must be >= 0")
        if occupancy.direction_min_votes < 1:
            raise ConfigError("occupancy.direction_min_votes must be >= 1")
        if occupancy.max_track_age_frames < 1:
            raise ConfigError("occupancy.max_track_age_frames must be >= 1")

        hogging = self.hogging
        if float(hogging.retrigger_after_seconds) < 0:
            raise ConfigError("hogging.retrigger_after_seconds must be >= 0")
        rule_names: set[str] = set()
        lane_roles = {lane.role for lane in self.lanes}
        for position, rule in enumerate(hogging.rules):
            label = f"hogging.rules[{position}]"
            if not str(rule.name).strip():
                raise ConfigError(f"{label}.name must not be empty")
            if rule.name in rule_names:
                raise ConfigError(f"{label}.name '{rule.name}' is duplicated")
            rule_names.add(rule.name)
            for key, values in (
                ("lanes", rule.lanes),
                ("lane_roles", rule.lane_roles),
                ("classes", rule.classes),
            ):
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise ConfigError(f"{label}.{key} must be a list of strings")
            if not rule.lanes and not rule.lane_roles:
                raise ConfigError(f"{label} must select at least one of lanes or lane_roles")
            unknown_lanes = sorted(set(rule.lanes) - seen_names)
            if unknown_lanes:
                raise ConfigError(f"{label}.lanes references unknown lane(s): {', '.join(unknown_lanes)}")
            unknown_roles = sorted(set(rule.lane_roles) - lane_roles)
            if unknown_roles:
                raise ConfigError(
                    f"{label}.lane_roles references unknown role(s): {', '.join(unknown_roles)}"
                )
            if any(not str(name).strip() for name in rule.classes):
                raise ConfigError(f"{label}.classes must contain non-empty class names")
            try:
                min_dwell = float(rule.min_dwell_seconds)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{label}.min_dwell_seconds must be a number") from exc
            if min_dwell < 0:
                raise ConfigError(f"{label}.min_dwell_seconds must be >= 0")
            if rule.severity not in SEVERITIES:
                raise ConfigError(f"{label}.severity must be one of: {', '.join(SEVERITIES)}")

        if self.analytics.interval_seconds <= 0:
            raise ConfigError("analytics.interval_seconds must be > 0")

        if not self.output.dir:
            raise ConfigError("output.dir must not be empty")

    def lane_map(self) -> dict[str, LaneConfig]:
        return {lane.name: lane for lane in self.lanes}

    def role_map(self) -> dict[str, list[LaneConfig]]:
        roles: dict[str, list[LaneConfig]] = {}
        for lane in self.lanes:
            roles.setdefault(lane.role, []).append(lane)
        return roles


def _section(cls: type, data: Any, label: str) -> Any:
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"'{label}' must be a mapping")
    allowed = {item.name for item in dataclasses.fields(cls)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"Unknown key(s) in '{label}': {', '.join(unknown)}")
    try:
        return cls(**data)
    except TypeError as exc:
        raise ConfigError(f"Invalid '{label}' section: {exc}") from exc


def _number(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} values must be numbers") from exc


def _polygon(data: Any, label: str) -> list[tuple[float, float]]:
    if not isinstance(data, list):
        raise ConfigError(f"{label} must be a list of [x, y] points")
    if len(data) < 3:
        raise ConfigError(f"{label} requires at least 3 points")
    points: list[tuple[float, float]] = []
    for index, item in enumerate(data):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ConfigError(f"{label}[{index}] must be a two-element list, e.g. [0.1, 0.5]")
        points.append((_number(item[0], label), _number(item[1], label)))
    return points


def _lanes(data: Any) -> list[LaneConfig]:
    if data is None:
        raise ConfigError("'lanes' is required and must be a non-empty list")
    if not isinstance(data, list) or not data:
        raise ConfigError("'lanes' must be a non-empty list")

    allowed = {item.name for item in dataclasses.fields(LaneConfig)}
    lanes: list[LaneConfig] = []
    for position, item in enumerate(data):
        label = f"lanes[{position}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be a mapping")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ConfigError(f"Unknown key(s) in {label}: {', '.join(unknown)}")
        if "polygon" not in item:
            raise ConfigError(f"{label} requires 'polygon'")
        kwargs = {key: value for key, value in item.items() if key != "polygon"}
        if "name" not in kwargs:
            raise ConfigError(f"{label} requires 'name'")
        if "index" in kwargs and kwargs["index"] is not None:
            try:
                kwargs["index"] = int(kwargs["index"])
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{label}.index must be an integer or null") from exc
        lanes.append(
            LaneConfig(polygon=_polygon(item["polygon"], f"{label}.polygon"), **kwargs)
        )
    return lanes


def _rules(data: Any) -> list[HoggingRuleConfig]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise ConfigError("'hogging.rules' must be a list")
    allowed = {item.name for item in dataclasses.fields(HoggingRuleConfig)}
    rules: list[HoggingRuleConfig] = []
    for position, item in enumerate(data):
        label = f"hogging.rules[{position}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be a mapping")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ConfigError(f"Unknown key(s) in {label}: {', '.join(unknown)}")
        if "name" not in item:
            raise ConfigError(f"{label} requires 'name'")
        kwargs = dict(item)
        for key in ("lanes", "lane_roles", "classes"):
            if key in kwargs and kwargs[key] is not None:
                value = kwargs[key]
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, (list, tuple)):
                    raise ConfigError(f"{label}.{key} must be a list of strings")
                kwargs[key] = [str(entry) for entry in value]
        if "min_dwell_seconds" in kwargs:
            try:
                kwargs["min_dwell_seconds"] = float(kwargs["min_dwell_seconds"])
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{label}.min_dwell_seconds must be a number") from exc
        if "severity" in kwargs:
            kwargs["severity"] = str(kwargs["severity"])
        rules.append(HoggingRuleConfig(**kwargs))
    return rules


def _hogging(data: Any) -> HoggingConfig:
    if data is None:
        return HoggingConfig()
    if not isinstance(data, dict):
        raise ConfigError("'hogging' must be a mapping")
    allowed = {"enabled", "retrigger_after_seconds", "rules"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"Unknown key(s) in 'hogging': {', '.join(unknown)}")
    enabled = data.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"false", "0", "no", "off"}
    try:
        retrigger = float(data.get("retrigger_after_seconds", 0.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("hogging.retrigger_after_seconds must be a number") from exc
    return HoggingConfig(
        enabled=bool(enabled),
        retrigger_after_seconds=retrigger,
        rules=_rules(data.get("rules")),
    )


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    """Load and validate a YAML configuration file."""
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping")

    allowed_top = {
        "camera_id",
        "base_timestamp",
        "model",
        "video",
        "lanes",
        "occupancy",
        "hogging",
        "analytics",
        "output",
    }
    unknown = sorted(set(raw) - allowed_top)
    if unknown:
        raise ConfigError(f"Unknown top-level key(s): {', '.join(unknown)}")

    config = AppConfig(
        camera_id=str(raw.get("camera_id", "camera_01")),
        base_timestamp=str(raw.get("base_timestamp", "") or ""),
        model=_section(ModelConfig, raw.get("model"), "model"),
        video=_section(VideoConfig, raw.get("video"), "video"),
        lanes=_lanes(raw.get("lanes")),
        occupancy=_section(OccupancyConfig, raw.get("occupancy"), "occupancy"),
        hogging=_hogging(raw.get("hogging")),
        analytics=_section(AnalyticsConfig, raw.get("analytics"), "analytics"),
        output=_section(OutputConfig, raw.get("output"), "output"),
    )
    config.validate()
    return config


def describe_config(config: AppConfig) -> str:
    """Human-readable one-block summary used by the CLI before a run."""
    lane_summary = ", ".join(
        f"{lane.name}({lane.role})" for lane in config.lanes
    )
    rule_summary = (
        ", ".join(rule.name for rule in config.hogging.rules) if config.hogging.rules else "(none)"
    )
    anchor_summary = config.occupancy.anchor
    if config.occupancy.anchor_shift_enabled:
        anchor_summary += (
            f" (adaptive: <= {config.occupancy.anchor_shift_small_bbox_px:.0f}px centre,"
            f" >= {config.occupancy.anchor_shift_large_bbox_px:.0f}px outer corner)"
        )
    return "\n".join(
        [
            f"camera_id       : {config.camera_id}",
            f"base timestamp  : {config.base_timestamp or '(run start time)'}",
            f"model           : {config.model.weights} ({config.model.device}, "
            f"imgsz={config.model.imgsz}, conf={config.model.conf})",
            f"source video    : {config.video.path}",
            f"frame stride    : {config.video.frame_stride}",
            f"lanes           : {lane_summary}",
            f"anchor          : {anchor_summary}",
            f"hogging         : {'enabled' if config.hogging.enabled else 'disabled'} "
            f"({rule_summary})",
        ]
    )


def as_plain_dict(config: AppConfig) -> dict[str, Any]:
    """JSON-friendly dump of the effective configuration (for run metadata)."""
    return dataclasses.asdict(config)
