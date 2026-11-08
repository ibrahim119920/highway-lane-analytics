"""Configurable lane-hogging rules.

A rule is an *indication*, not an automated enforcement decision. It matches a
lane (by name or role), a vehicle class, and a minimum continuous dwell time.
Rules that do not match are simply allowed usage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .config import HoggingRuleConfig
from .geometry import LaneRegion

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True)
class HoggingRule:
    name: str
    lanes: frozenset[str]
    lane_roles: frozenset[str]
    classes: frozenset[str]
    min_dwell_seconds: float
    lane_indices: frozenset[int] = frozenset()
    exempt_when_overtaking: bool = False
    severity: str = "medium"
    description: str = ""

    def matches(self, lane: LaneRegion, object_class: str) -> bool:
        if lane.index is not None and lane.index in self.lane_indices:
            lane_match = True
        else:
            lane_match = lane.name in self.lanes or lane.role in self.lane_roles
        if not lane_match:
            return False
        if self.classes and object_class not in self.classes:
            return False
        return True

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 0)

    def message(self, lane: LaneRegion, object_class: str) -> str:
        if self.description:
            return self.description
        return (
            f"{object_class} berada di {lane.name} ({lane.role}) "
            f"selama {self.min_dwell_seconds:.1f}s"
        )


def build_rules(configs: Iterable[HoggingRuleConfig]) -> list[HoggingRule]:
    return [
        HoggingRule(
            name=config.name,
            lanes=frozenset(config.lanes),
            lane_roles=frozenset(config.lane_roles),
            lane_indices=frozenset(config.lane_indices),
            classes=frozenset(config.classes),
            min_dwell_seconds=float(config.min_dwell_seconds),
            exempt_when_overtaking=bool(config.exempt_when_overtaking),
            severity=config.severity,
            description=config.description,
        )
        for config in configs
    ]


def active_rule(rules: Iterable[HoggingRule], lane: LaneRegion, object_class: str) -> HoggingRule | None:
    """Return the most severe matching rule, or ``None`` when usage is allowed."""
    matches = [rule for rule in rules if rule.matches(lane, object_class)]
    if not matches:
        return None
    return max(matches, key=lambda rule: (rule.rank, -rule.min_dwell_seconds))
