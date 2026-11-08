"""Tests for small shared helpers."""

from __future__ import annotations

from pathlib import Path

from lane_analytics.utils import ensure_dir


def test_ensure_dir_accepts_string(tmp_path: Path):
    target = tmp_path / "nested" / "dir"
    returned = ensure_dir(str(target))
    assert returned == target
    assert target.is_dir()


def test_ensure_dir_accepts_path(tmp_path: Path):
    target = tmp_path / "other"
    returned = ensure_dir(target)
    assert returned == target
    assert target.is_dir()
