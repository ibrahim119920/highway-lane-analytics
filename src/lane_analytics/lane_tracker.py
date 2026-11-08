"""Lane occupancy, lane-change, and lane-hogging tracking."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .geometry import LaneRegion, assign_lane
from .hogging import HoggingRule, active_rule
from .models import LaneEvent, LaneStint, TrackSummary, TrackedObject


@dataclass
class _TrackState:
    track_id: int
    class_id: int
    class_name: str
    confidence: float
    first_frame: int
    last_frame: int
    first_timestamp: str
    last_timestamp: str
    first_time_s: float
    last_time_s: float
    lane: LaneRegion | None = None
    stint: LaneStint | None = None
    class_votes: Counter[str] = field(default_factory=Counter)
    class_ids: dict[str, int] = field(default_factory=dict)
    direction: str = ""
    direction_votes: dict[str, int] = field(
        default_factory=lambda: {"northbound": 0, "southbound": 0}
    )
    previous_bottom_y: float | None = None
    anchor_point: tuple[float, float] | None = None
    pending_active: bool = False
    pending_lane: LaneRegion | None = None
    pending_since_frame: int = 0
    pending_since_time_s: float = 0.0
    pending_timestamp: str = ""
    stints: list[LaneStint] = field(default_factory=list)
    lane_changes: int = 0
    hogging_count: int = 0
    triggered: dict[tuple[int, str], float] = field(default_factory=dict)
    active_hogging_rule: str = ""
    active_hogging_severity: str = ""
    summarized: bool = False


class LaneTracker:
    """Assign every tracked vehicle to a lane and analyze its lane behaviour.

    The tracker is stateful and expects frames in increasing order. It emits
    events for lane entry, lane exit, lane changes, and configured lane-hogging
    indications. A short stability window (``min_lane_frames``) prevents
    one-frame polygon-boundary flicker from creating false lane changes.
    """

    def __init__(
        self,
        lanes: Sequence[LaneRegion],
        *,
        frame_width: int,
        frame_height: int,
        camera_id: str = "",
        anchor: str = "bottom_center",
        anchor_shift_enabled: bool = False,
        anchor_shift_small_bbox_px: float = 90.0,
        anchor_shift_large_bbox_px: float = 250.0,
        direction_deadband_px: float = 2.0,
        direction_min_votes: int = 2,
        min_confidence: float = 0.3,
        min_lane_frames: int = 2,
        max_track_age_frames: int = 90,
        emit_lane_enter: bool = True,
        emit_lane_exit: bool = True,
        emit_lane_change: bool = True,
        hogging_enabled: bool = True,
        hogging_rules: Sequence[HoggingRule] = (),
        retrigger_after_seconds: float = 0.0,
    ) -> None:
        if not lanes:
            raise ValueError("At least one lane region is required")
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("frame_width and frame_height must be positive")
        self._lanes = list(lanes)
        self._frame_width = int(frame_width)
        self._frame_height = int(frame_height)
        self._camera_id = camera_id
        self._anchor = anchor
        self._anchor_shift_enabled = bool(anchor_shift_enabled)
        self._anchor_shift_small = float(anchor_shift_small_bbox_px)
        self._anchor_shift_large = float(anchor_shift_large_bbox_px)
        self._direction_deadband = max(0.0, float(direction_deadband_px))
        self._direction_min_votes = max(1, int(direction_min_votes))
        self._min_confidence = float(min_confidence)
        self._min_lane_frames = max(1, int(min_lane_frames))
        self._max_age = max(1, int(max_track_age_frames))
        self._emit_lane_enter = bool(emit_lane_enter)
        self._emit_lane_exit = bool(emit_lane_exit)
        self._emit_lane_change = bool(emit_lane_change)
        self._hogging_enabled = bool(hogging_enabled) and bool(hogging_rules)
        self._rules = list(hogging_rules)
        self._retrigger_after = max(0.0, float(retrigger_after_seconds))

        self._states: dict[int, _TrackState] = {}
        self._summaries: list[TrackSummary] = []
        self._next_stint_id = 1
        self._tracks_seen = 0
        self._event_count = 0
        self._lane_change_count = 0
        self._hogging_count = 0

    # --------------------------------------------------------------- properties
    @property
    def lanes(self) -> list[LaneRegion]:
        return list(self._lanes)

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def lane_change_count(self) -> int:
        return self._lane_change_count

    @property
    def hogging_count(self) -> int:
        return self._hogging_count

    # ------------------------------------------------------------------- update
    def update(
        self,
        objects: Iterable[TrackedObject],
        *,
        frame_index: int,
        timestamp: str,
        video_time_s: float,
    ) -> list[LaneEvent]:
        """Update track state for one processed frame and return new events."""
        events: list[LaneEvent] = []
        events.extend(self._prune(frame_index))

        for obj in objects:
            if obj.confidence < self._min_confidence:
                continue
            state = self._states.get(obj.track_id)
            if state is None:
                state = _TrackState(
                    track_id=int(obj.track_id),
                    class_id=int(obj.class_id),
                    class_name=str(obj.class_name),
                    confidence=float(obj.confidence),
                    first_frame=int(frame_index),
                    last_frame=int(frame_index),
                    first_timestamp=timestamp,
                    last_timestamp=timestamp,
                    first_time_s=float(video_time_s),
                    last_time_s=float(video_time_s),
                )
                self._states[state.track_id] = state
                self._tracks_seen += 1

            # Vote on the class instead of trusting the latest frame: a single
            # "bus" frame inside a car track must not trigger a heavy-vehicle rule.
            observed_name = str(obj.class_name)
            state.class_votes[observed_name] += 1
            state.class_ids[observed_name] = int(obj.class_id)
            dominant = state.class_votes.most_common(1)[0][0]
            state.class_name = dominant
            state.class_id = state.class_ids.get(dominant, int(obj.class_id))
            state.confidence = float(obj.confidence)
            state.last_frame = int(frame_index)
            state.last_timestamp = timestamp
            state.last_time_s = float(video_time_s)

            # Travel direction from vertical box motion: receding (northbound) traffic
            # moves up (y decreasing), approaching (southbound) traffic moves down.
            # Require several consistent observations so a one-frame box jitter cannot
            # lock the wrong direction.
            bottom_y = float(obj.bbox[3])
            if not state.direction and state.previous_bottom_y is not None:
                delta_y = bottom_y - state.previous_bottom_y
                if delta_y > self._direction_deadband:
                    state.direction_votes["southbound"] += 1
                elif delta_y < -self._direction_deadband:
                    state.direction_votes["northbound"] += 1
                north = state.direction_votes["northbound"]
                south = state.direction_votes["southbound"]
                if max(north, south) >= self._direction_min_votes and north != south:
                    state.direction = "northbound" if north > south else "southbound"
            state.previous_bottom_y = bottom_y

            candidate = self._lane_for(obj, state)
            events.extend(
                self._apply_candidate(
                    state,
                    candidate,
                    frame_index=int(frame_index),
                    timestamp=timestamp,
                    time_s=float(video_time_s),
                )
            )
            events.extend(
                self._evaluate_hogging(
                    state,
                    frame_index=int(frame_index),
                    timestamp=timestamp,
                    time_s=float(video_time_s),
                )
            )

        self._event_count += len(events)
        return events

    def finalize(
        self,
        *,
        frame_index: int,
        timestamp: str,
        video_time_s: float,
    ) -> list[LaneEvent]:
        """Close every open lane stint at the last observation time."""
        events: list[LaneEvent] = []
        for state in list(self._states.values()):
            events.extend(
                self._finalize_track(
                    state,
                    fallback_frame=int(frame_index),
                    fallback_timestamp=timestamp,
                    fallback_time_s=float(video_time_s),
                )
            )
        self._states.clear()
        self._event_count += len(events)
        return events

    # ------------------------------------------------------------- lane matching
    def _lane_for(self, obj: TrackedObject, state: _TrackState) -> LaneRegion | None:
        if self._anchor_shift_enabled:
            anchor_x, anchor_y = obj.directional_anchor(
                frame_width=self._frame_width,
                small_px=self._anchor_shift_small,
                large_px=self._anchor_shift_large,
                fallback=self._anchor,
            )
        else:
            anchor_x, anchor_y = obj.anchor(self._anchor)
        state.anchor_point = (anchor_x, anchor_y)
        point = (anchor_x / self._frame_width, anchor_y / self._frame_height)
        return assign_lane(point, self._lanes)

    @staticmethod
    def _name(lane: LaneRegion | None) -> str | None:
        return lane.name if lane is not None else None

    # --------------------------------------------------------------- transitions
    def _apply_candidate(
        self,
        state: _TrackState,
        candidate: LaneRegion | None,
        *,
        frame_index: int,
        timestamp: str,
        time_s: float,
    ) -> list[LaneEvent]:
        current_name = self._name(state.lane)
        candidate_name = self._name(candidate)

        if candidate_name == current_name and not state.pending_active:
            return []
        if candidate_name == current_name:
            # The vehicle moved back to the current lane before the switch
            # became stable: cancel the pending transition.
            state.pending_active = False
            state.pending_lane = None
            return []

        pending_name = self._name(state.pending_lane) if state.pending_active else None
        if not state.pending_active or pending_name != candidate_name:
            state.pending_active = True
            state.pending_lane = candidate
            state.pending_since_frame = int(frame_index)
            state.pending_since_time_s = float(time_s)
            state.pending_timestamp = timestamp

        # ``min_lane_frames`` counts the pending observation itself, so two
        # frames of evidence means the second consecutive frame commits.
        waited = int(frame_index) - state.pending_since_frame
        if waited < self._min_lane_frames - 1:
            return []

        # Commit the transition at the moment the new lane was first observed.
        enter_frame = state.pending_since_frame
        enter_time = state.pending_since_time_s
        enter_timestamp = state.pending_timestamp
        previous = state.lane
        previous_stint = state.stint

        state.pending_active = False
        state.pending_lane = None
        events: list[LaneEvent] = []
        previous_dwell = 0.0

        if previous is not None and previous_stint is not None:
            previous_dwell = previous_stint.close(
                frame_index=enter_frame,
                time_s=enter_time,
                timestamp=enter_timestamp,
            )
            if self._emit_lane_exit:
                events.append(
                    self._event(
                        state,
                        event_type="lane_exit",
                        lane=previous,
                        confidence=state.confidence,
                        frame_index=enter_frame,
                        timestamp=enter_timestamp,
                        time_s=enter_time,
                        dwell_seconds=previous_dwell,
                    )
                )
            state.stint = None

        if candidate is not None:
            stint = LaneStint(
                stint_id=self._next_stint_id,
                lane_name=candidate.name,
                lane_index=candidate.index,
                lane_role=candidate.role,
                enter_frame=enter_frame,
                enter_time_s=round(enter_time, 3),
                enter_timestamp=enter_timestamp,
            )
            self._next_stint_id += 1
            state.stint = stint
            state.stints.append(stint)
            state.lane = candidate
            if previous is not None:
                state.lane_changes += 1
                self._lane_change_count += 1
                if self._emit_lane_change:
                    events.append(
                        self._event(
                            state,
                            event_type="lane_change",
                            lane=candidate,
                            confidence=state.confidence,
                            frame_index=enter_frame,
                            timestamp=enter_timestamp,
                            time_s=enter_time,
                            dwell_seconds=previous_dwell,
                            previous_lane=previous.name,
                        )
                    )
            if self._emit_lane_enter:
                events.append(
                    self._event(
                        state,
                        event_type="lane_enter",
                        lane=candidate,
                        confidence=state.confidence,
                        frame_index=enter_frame,
                        timestamp=enter_timestamp,
                        time_s=enter_time,
                        previous_lane=previous.name if previous else "",
                    )
                )
        else:
            state.lane = None
            state.stint = None
        return events

    # ------------------------------------------------------------------ hogging
    def _evaluate_hogging(
        self,
        state: _TrackState,
        *,
        frame_index: int,
        timestamp: str,
        time_s: float,
    ) -> list[LaneEvent]:
        state.active_hogging_rule = ""
        state.active_hogging_severity = ""
        if not self._hogging_enabled or state.lane is None or state.stint is None:
            return []
        # Do not flag a vehicle that is already visibly moving to another lane.
        if state.pending_active and self._name(state.pending_lane) != state.lane.name:
            return []

        dwell = max(0.0, float(time_s) - state.stint.enter_time_s)
        rule = active_rule(self._rules, state.lane, state.class_name)
        if rule is None or dwell + 1e-9 < rule.min_dwell_seconds:
            return []

        state.active_hogging_rule = rule.name
        state.active_hogging_severity = rule.severity
        key = (state.stint.stint_id, rule.name)
        last_triggered = state.triggered.get(key)
        if last_triggered is not None:
            if self._retrigger_after <= 0:
                return []
            if dwell - last_triggered + 1e-9 < self._retrigger_after:
                return []

        state.triggered[key] = dwell
        state.hogging_count += 1
        self._hogging_count += 1
        return [
            self._event(
                state,
                event_type="lane_hogging_indication",
                lane=state.lane,
                confidence=state.confidence,
                frame_index=frame_index,
                timestamp=timestamp,
                time_s=time_s,
                dwell_seconds=dwell,
                severity=rule.severity,
                rule_name=rule.name,
                message=rule.message(state.lane, state.class_name),
            )
        ]

    # ---------------------------------------------------------------- finalizing
    def _prune(self, frame_index: int) -> list[LaneEvent]:
        stale = [
            state
            for state in self._states.values()
            if int(frame_index) - state.last_frame > self._max_age
        ]
        events: list[LaneEvent] = []
        for state in stale:
            events.extend(
                self._finalize_track(
                    state,
                    fallback_frame=state.last_frame,
                    fallback_timestamp=state.last_timestamp,
                    fallback_time_s=state.last_time_s,
                )
            )
            self._states.pop(state.track_id, None)
        return events

    def _finalize_track(
        self,
        state: _TrackState,
        *,
        fallback_frame: int,
        fallback_timestamp: str,
        fallback_time_s: float,
    ) -> list[LaneEvent]:
        events: list[LaneEvent] = []
        if state.stint is not None and state.stint.open:
            end_frame = state.last_frame if state.last_frame >= state.stint.enter_frame else fallback_frame
            end_time = state.last_time_s if state.last_time_s >= state.stint.enter_time_s else fallback_time_s
            end_timestamp = state.last_timestamp or fallback_timestamp
            dwell = state.stint.close(
                frame_index=end_frame,
                time_s=end_time,
                timestamp=end_timestamp,
            )
            if self._emit_lane_exit and state.lane is not None:
                events.append(
                    self._event(
                        state,
                        event_type="lane_exit",
                        lane=state.lane,
                        confidence=state.confidence,
                        frame_index=end_frame,
                        timestamp=end_timestamp,
                        time_s=end_time,
                        dwell_seconds=dwell,
                    )
                )
            state.stint = None

        if not state.summarized:
            state.summarized = True
            self._summaries.append(self._build_summary(state))
        return events

    def _build_summary(self, state: _TrackState) -> TrackSummary:
        dwell_by_lane: Counter[str] = Counter()
        for stint in state.stints:
            dwell_by_lane[stint.lane_name] += stint.dwell_seconds
        dominant = ""
        if dwell_by_lane:
            dominant = max(dwell_by_lane.items(), key=lambda item: (item[1], item[0]))[0]
        return TrackSummary(
            camera_id=self._camera_id,
            track_id=state.track_id,
            object_class=state.class_name,
            class_id=state.class_id,
            first_frame=state.first_frame,
            last_frame=state.last_frame,
            first_timestamp=state.first_timestamp,
            last_timestamp=state.last_timestamp,
            first_video_time_s=round(state.first_time_s, 3),
            last_video_time_s=round(state.last_time_s, 3),
            lane_stints=list(state.stints),
            lane_change_count=state.lane_changes,
            hogging_event_count=state.hogging_count,
            dominant_lane=dominant,
            total_lane_dwell_seconds=round(sum(dwell_by_lane.values()), 3),
        )

    # ------------------------------------------------------------------ reading
    def track_summaries(self) -> list[TrackSummary]:
        return list(self._summaries)

    def status_by_track(self) -> dict[int, dict[str, object]]:
        """Current per-track lane status used by the annotator."""
        status: dict[int, dict[str, object]] = {}
        for track_id, state in self._states.items():
            dwell = 0.0
            if state.stint is not None:
                dwell = max(0.0, state.last_time_s - state.stint.enter_time_s)
            status[track_id] = {
                "lane": state.lane.name if state.lane else "",
                "lane_index": state.lane.index if state.lane else None,
                "lane_role": state.lane.role if state.lane else "",
                "dwell_seconds": round(dwell, 2),
                "hogging": bool(state.active_hogging_rule),
                "severity": state.active_hogging_severity,
                "rule": state.active_hogging_rule,
                "lane_changes": state.lane_changes,
                "anchor": state.anchor_point,
                "direction": state.direction,
            }
        return status

    def snapshot(self) -> dict[str, object]:
        """Aggregate live counters for the HUD."""
        by_lane: Counter[str] = Counter()
        by_class: Counter[str] = Counter()
        for state in self._states.values():
            if state.lane is not None:
                by_lane[state.lane.name] += 1
            by_class[state.class_name] += 1
        return {
            "tracks_seen": int(self._tracks_seen),
            "active_tracks": int(len(self._states)),
            "completed_tracks": int(len(self._summaries)),
            "by_lane": dict(by_lane),
            "by_class": dict(by_class),
            "events": int(self._event_count),
            "lane_changes": int(self._lane_change_count),
            "hogging_indications": int(self._hogging_count),
        }

    # ------------------------------------------------------------------- helper
    def _event(
        self,
        state: _TrackState,
        *,
        event_type: str,
        lane: LaneRegion,
        confidence: float,
        frame_index: int,
        timestamp: str,
        time_s: float,
        dwell_seconds: float = 0.0,
        previous_lane: str = "",
        severity: str = "",
        rule_name: str = "",
        message: str = "",
    ) -> LaneEvent:
        return LaneEvent(
            camera_id=self._camera_id,
            timestamp=timestamp,
            track_id=state.track_id,
            object_class=state.class_name,
            confidence=round(float(confidence), 4),
            event_type=event_type,
            lane_name=lane.name,
            lane_index=lane.index,
            lane_role=lane.role,
            previous_lane=previous_lane,
            dwell_seconds=round(max(0.0, float(dwell_seconds)), 3),
            class_id=state.class_id,
            frame_index=int(frame_index),
            video_time_s=round(float(time_s), 3),
            severity=severity,
            rule_name=rule_name,
            message=message,
        )
