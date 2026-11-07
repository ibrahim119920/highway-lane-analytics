"""Tests for YAML config loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from lane_analytics.config import ConfigError, load_config


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


VALID = """
camera_id: camera_01
model:
  weights: yolo11n.pt
  classes: [2, 5, 7]
video:
  path: data/raw/traffic.mp4
  frame_stride: 2
lanes:
  - name: lane_left
    index: 1
    direction: northbound
    role: normal
    polygon: [[0.05, 0.1], [0.35, 0.1], [0.35, 0.95], [0.05, 0.95]]
  - name: lane_right
    index: 2
    direction: northbound
    role: overtaking
    polygon: [[0.65, 0.1], [0.95, 0.1], [0.95, 0.95], [0.65, 0.95]]
occupancy:
  anchor: bottom_center
  min_lane_frames: 2
hogging:
  enabled: true
  rules:
    - name: heavy_vehicle_in_overtaking_lane
      lane_roles: [overtaking]
      classes: [truck, bus]
      min_dwell_seconds: 3.0
      severity: high
analytics:
  interval_seconds: 60
"""


def test_load_valid_config(tmp_path: Path):
    config = load_config(write_config(tmp_path, VALID))
    assert config.camera_id == "camera_01"
    assert config.video.frame_stride == 2
    assert [lane.name for lane in config.lanes] == ["lane_left", "lane_right"]
    assert config.lanes[1].role == "overtaking"
    assert config.lanes[1].polygon[0] == (0.65, 0.1)
    assert config.occupancy.anchor == "bottom_center"
    assert config.hogging.rules[0].severity == "high"


def test_unknown_top_level_key_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="Unknown top-level"):
        load_config(write_config(tmp_path, VALID + "\nnot_a_setting: 1\n"))


def test_unknown_nested_key_is_rejected(tmp_path: Path):
    text = VALID.replace("frame_stride: 2", "frame_stride: 2\n  mystery: true")
    with pytest.raises(ConfigError, match="Unknown key"):
        load_config(write_config(tmp_path, text))


def test_zero_frame_stride_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="frame_stride"):
        load_config(write_config(tmp_path, VALID.replace("frame_stride: 2", "frame_stride: 0")))


def test_normalized_polygon_coordinates_are_enforced(tmp_path: Path):
    with pytest.raises(ConfigError, match="normalized"):
        load_config(
            write_config(
                tmp_path,
                VALID.replace("[0.65, 0.1]", "[1.5, 0.1]"),
            )
        )


def test_duplicate_lane_names_are_rejected(tmp_path: Path):
    duplicate = VALID.replace("name: lane_right", "name: lane_left")
    with pytest.raises(ConfigError, match="duplicated"):
        load_config(write_config(tmp_path, duplicate))


def test_lane_index_is_scoped_per_direction(tmp_path: Path):
    opposite = """
  - name: southbound_lane_1
    index: 1
    direction: southbound
    role: normal
    polygon: [[0.05, 0.55], [0.35, 0.55], [0.35, 0.95], [0.05, 0.95]]
"""
    config = load_config(write_config(tmp_path, VALID.replace("occupancy:", opposite + "occupancy:")))
    assert [(lane.name, lane.direction, lane.index) for lane in config.lanes] == [
        ("lane_left", "northbound", 1),
        ("lane_right", "northbound", 2),
        ("southbound_lane_1", "southbound", 1),
    ]


def test_same_index_in_the_same_direction_is_rejected(tmp_path: Path):
    duplicate = VALID.replace("    index: 2\n", "    index: 1\n")
    with pytest.raises(ConfigError, match="duplicated for direction 'northbound'"):
        load_config(write_config(tmp_path, duplicate))


def test_missing_polygon_is_rejected(tmp_path: Path):
    text = VALID.replace(
        "    polygon: [[0.05, 0.1], [0.35, 0.1], [0.35, 0.95], [0.05, 0.95]]\n", ""
    )
    with pytest.raises(ConfigError, match="requires 'polygon'"):
        load_config(write_config(tmp_path, text))


def test_rule_must_select_lane_or_role(tmp_path: Path):
    text = VALID.replace("      lane_roles: [overtaking]\n", "")
    with pytest.raises(ConfigError, match="at least one of lanes or lane_roles"):
        load_config(write_config(tmp_path, text))


def test_rule_cannot_reference_unknown_lane(tmp_path: Path):
    text = VALID.replace("      lane_roles: [overtaking]", "      lanes: [missing_lane]")
    with pytest.raises(ConfigError, match="unknown lane"):
        load_config(write_config(tmp_path, text))


def test_invalid_severity_is_rejected(tmp_path: Path):
    text = VALID.replace("severity: high", "severity: critical")
    with pytest.raises(ConfigError, match="severity"):
        load_config(write_config(tmp_path, text))


def test_single_string_class_is_normalized_to_list(tmp_path: Path):
    text = VALID.replace("classes: [truck, bus]", "classes: truck")
    config = load_config(write_config(tmp_path, text))
    assert config.hogging.rules[0].classes == ["truck"]


def test_rule_classes_must_be_a_list_or_string(tmp_path: Path):
    text = VALID.replace("classes: [truck, bus]", "classes: {truck: true}")
    with pytest.raises(ConfigError, match="classes"):
        load_config(write_config(tmp_path, text))


def test_invalid_anchor_is_rejected(tmp_path: Path):
    text = VALID.replace("anchor: bottom_center", "anchor: middle")
    with pytest.raises(ConfigError, match="anchor"):
        load_config(write_config(tmp_path, text))


def test_missing_file_has_clear_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml")
