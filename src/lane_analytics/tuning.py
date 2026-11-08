"""Ground-truth tuning for lane-hogging rules.

The manual label sheet (``data/labels/lane_hogging_10m.csv``) marks, per vehicle,
whether it was cruising in lane 2/3 (hogging) or actively overtaking. This module
fits the rule thresholds against those labels in two ways:

* ``--frame-log`` replays the exact per-frame detections through the real
  :class:`~lane_analytics.lane_tracker.LaneTracker`, so bbox lane assignment and
  overtake context are evaluated exactly as in production.
* ``--tracks`` uses the per-track lane stints from an existing run and a fast
  overlap proxy (a lane-3 vehicle is "overtaking" when another vehicle occupied
  the adjacent inner lane for most of its stint).

Both modes report precision/recall/F1 against the manual labels.
"""

from __future__ import annotations

import copy
import csv
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from .config import AppConfig, HoggingRuleConfig, as_plain_dict, load_config
from .models import TrackedObject
from .pipeline import create_lane_tracker
from .utils import ensure_dir

HOGGING_LANES = (2, 3)
HEAVY_CLASSES = ("truck", "bus")
LIGHT_CLASSES = ("car",)


@dataclass(frozen=True)
class GroundTruthVehicle:
    key: str
    track_ids: tuple[int, ...]
    direction: str
    object_class: str
    lane_index: int
    hogging: bool
    reason: str = ""


@dataclass
class Metrics:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        if not (self.precision + self.recall):
            return 0.0
        return 2 * self.precision * self.recall / (self.precision + self.recall)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


@dataclass
class Prediction:
    key: str
    predicted: bool
    label: bool
    object_class: str
    lane_index: int
    dwell_seconds: float = 0.0
    lane_names: str = ""
    note: str = ""

    @property
    def outcome(self) -> str:
        if self.predicted and self.label:
            return "TP"
        if self.predicted and not self.label:
            return "FP"
        if not self.predicted and self.label:
            return "FN"
        return "TN"


@dataclass
class Params:
    heavy_dwell: float = 0.4
    car_dwell: float = 0.4
    assignment: str = "anchor"
    overtake_exemption: bool = True
    overtake_gap_fraction: float = 0.10
    overtake_gap_back_fraction: float = 0.50
    overtake_speed_margin_px_s: float = 3.0
    overtake_confirm_seconds: float = 0.2
    overtake_stint_memory: bool = True
    evaluate_at: str = "stint"
    overlap_ratio_max: float = 0.8

    def to_dict(self) -> dict[str, Any]:
        return {
            "heavy_dwell_seconds": round(self.heavy_dwell, 3),
            "car_dwell_seconds": round(self.car_dwell, 3),
            "assignment": self.assignment,
            "overtake_exemption": self.overtake_exemption,
            "overtake_gap_fraction": round(self.overtake_gap_fraction, 3),
            "overtake_gap_back_fraction": round(self.overtake_gap_back_fraction, 3),
            "overtake_speed_margin_px_s": round(self.overtake_speed_margin_px_s, 3),
            "overtake_confirm_seconds": round(self.overtake_confirm_seconds, 3),
            "overtake_stint_memory": self.overtake_stint_memory,
            "evaluate_at": self.evaluate_at,
            "overlap_ratio_max": round(self.overlap_ratio_max, 3),
        }

    def label(self) -> str:
        return (
            f"heavy={self.heavy_dwell:.1f}s car={self.car_dwell:.1f}s "
            f"assign={self.assignment} evaluate={self.evaluate_at} "
            f"overtake={'on' if self.overtake_exemption else 'off'} "
            f"gap={self.overtake_gap_fraction:.2f} back={self.overtake_gap_back_fraction:.2f} "
            f"margin={self.overtake_speed_margin_px_s:.0f}px/s "
            f"confirm={self.overtake_confirm_seconds:.1f}s "
            f"memory={'on' if self.overtake_stint_memory else 'off'} "
            f"ratio<{self.overlap_ratio_max:.2f}"
        )


@dataclass
class Outcome:
    params: Params
    metrics: Metrics
    predictions: list[Prediction] = field(default_factory=list)
    mode: str = ""
    notes: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    class_results: dict[int, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- labels
def load_ground_truth(path: str | Path) -> list[GroundTruthVehicle]:
    """Load the manual label CSV, grouping split track ids into one vehicle."""
    rows: dict[str, list[dict[str, str]]] = {}
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.setdefault(row["vehicle_key"], []).append(row)
    vehicles: list[GroundTruthVehicle] = []
    for key, group in rows.items():
        first = group[0]
        vehicles.append(
            GroundTruthVehicle(
                key=key,
                track_ids=tuple(int(item["track_id"]) for item in group),
                direction=first["direction"],
                object_class=first["object_class"],
                lane_index=int(first["lane_index"]),
                hogging=first["hogging"].strip().lower() == "true",
                reason=first.get("reason", ""),
            )
        )
    return sorted(vehicles, key=lambda item: item.track_ids[0])


# --------------------------------------------------------------------------- params
def _rules_for(params: Params) -> list[HoggingRuleConfig]:
    return [
        HoggingRuleConfig(
            name="heavy_cruising_lane_2_3",
            lane_indices=list(HOGGING_LANES),
            classes=list(HEAVY_CLASSES),
            min_dwell_seconds=params.heavy_dwell,
            exempt_when_overtaking=params.overtake_exemption,
            severity="high",
            description="Truk/bus cruising di lajur 2/3 tanpa sedang mendahului.",
        ),
        HoggingRuleConfig(
            name="car_cruising_lane_2_3",
            lane_indices=list(HOGGING_LANES),
            classes=list(LIGHT_CLASSES),
            min_dwell_seconds=params.car_dwell,
            exempt_when_overtaking=params.overtake_exemption,
            severity="medium",
            description="Mobil cruising di lajur 2/3 tanpa sedang mendahului.",
        ),
    ]


def apply_params(config: AppConfig, params: Params) -> AppConfig:
    tuned = copy.deepcopy(config)
    tuned.occupancy.assignment = params.assignment
    tuned.hogging.overtake_exemption = params.overtake_exemption
    tuned.hogging.overtake_gap_fraction = params.overtake_gap_fraction
    tuned.hogging.overtake_gap_back_fraction = params.overtake_gap_back_fraction
    tuned.hogging.overtake_speed_margin_px_s = params.overtake_speed_margin_px_s
    tuned.hogging.overtake_confirm_seconds = params.overtake_confirm_seconds
    tuned.hogging.overtake_stint_memory = params.overtake_stint_memory
    tuned.hogging.evaluate_at = params.evaluate_at
    tuned.hogging.rules = _rules_for(params)
    return tuned


# --------------------------------------------------------------------------- scoring
def _score(
    labels: Sequence[GroundTruthVehicle],
    predicted_ids: set[int],
    details: dict[int, dict[str, Any]] | None = None,
    observed_ids: set[int] | None = None,
) -> tuple[Metrics, list[Prediction], list[str]]:
    metrics = Metrics()
    predictions: list[Prediction] = []
    excluded: list[str] = []
    details = details or {}
    for label in labels:
        if observed_ids is not None and not any(
            track_id in observed_ids for track_id in label.track_ids
        ):
            excluded.append(label.key)
            continue
        hit = any(track_id in predicted_ids for track_id in label.track_ids)
        if hit and label.hogging:
            metrics.tp += 1
        elif hit and not label.hogging:
            metrics.fp += 1
        elif not hit and label.hogging:
            metrics.fn += 1
        info: dict[str, Any] = {}
        for track_id in label.track_ids:
            if track_id in details:
                info = details[track_id]
                break
        predictions.append(
            Prediction(
                key=label.key,
                predicted=hit,
                label=label.hogging,
                object_class=label.object_class,
                lane_index=label.lane_index,
                dwell_seconds=float(info.get("dwell_seconds", 0.0)),
                lane_names=str(info.get("lane_names", "")),
                note=label.reason,
            )
        )
    return metrics, predictions, excluded


# ---------------------------------------------------------------- frame-log replay
def _load_frame_log(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _frame_log_size(records: Iterable[dict[str, Any]], width: int, height: int) -> tuple[int, int]:
    max_x = 0.0
    max_y = 0.0
    for record in records:
        for obj in record.get("objects", []):
            bbox = obj.get("bbox") or (0, 0, 0, 0)
            max_x = max(max_x, float(bbox[2]))
            max_y = max(max_y, float(bbox[3]))
    return (
        width or max(1, int(round(max_x))),
        height or max(1, int(round(max_y))),
    )


def evaluate_frame_log(
    config: AppConfig,
    records: Sequence[dict[str, Any]],
    labels: Sequence[GroundTruthVehicle],
    params: Params,
    *,
    width: int = 0,
    height: int = 0,
) -> Outcome:
    tuned = apply_params(config, params)
    tuned.validate()
    frame_width, frame_height = _frame_log_size(records, width, height)
    tracker = create_lane_tracker(
        tuned, frame_width=frame_width, frame_height=frame_height
    )
    last_record: dict[str, Any] | None = None
    for record in records:
        objects = [
            TrackedObject(
                track_id=int(item["track_id"]),
                class_id=int(item.get("class_id", 0)),
                class_name=str(item.get("class_name", "")),
                confidence=float(item.get("confidence", 1.0)),
                bbox=tuple(float(value) for value in item["bbox"]),
            )
            for item in record.get("objects", [])
        ]
        tracker.update(
            objects,
            frame_index=int(record.get("frame_index", 0)),
            timestamp=str(record.get("timestamp", "")),
            video_time_s=float(record.get("video_time_s", 0.0)),
        )
        last_record = record
    if last_record is not None:
        tracker.finalize(
            frame_index=int(last_record.get("frame_index", 0)),
            timestamp=str(last_record.get("timestamp", "")),
            video_time_s=float(last_record.get("video_time_s", 0.0)),
        )

    predicted_ids: set[int] = set()
    details: dict[int, dict[str, Any]] = {}
    observed_ids: set[int] = set()
    class_results: dict[int, str] = {}
    for summary in tracker.track_summaries():
        observed_ids.add(summary.track_id)
        class_results[summary.track_id] = summary.object_class
        stints = [stint for stint in summary.lane_stints if (stint.lane_index or 0) >= 2]
        dwell = max((stint.dwell_seconds for stint in stints), default=0.0)
        details[summary.track_id] = {
            "dwell_seconds": dwell,
            "lane_names": ", ".join(stint.lane_name for stint in stints),
        }
        if summary.hogging_event_count:
            predicted_ids.add(summary.track_id)
    metrics, predictions, excluded = _score(
        labels, predicted_ids, details, observed_ids=observed_ids
    )
    return Outcome(
        params=params,
        metrics=metrics,
        predictions=predictions,
        mode="frame-log",
        excluded=excluded,
        class_results=class_results,
    )


# --------------------------------------------------------------- track-summary proxy
def load_track_summaries(path: str | Path) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                summaries.append(json.loads(line))
    return summaries


def _overlap(first: tuple[float, float], second: tuple[float, float]) -> float:
    return max(0.0, min(first[1], second[1]) - max(first[0], second[0]))


def evaluate_track_summaries(
    summaries: Sequence[dict[str, Any]],
    labels: Sequence[GroundTruthVehicle],
    params: Params,
) -> Outcome:
    """Fast evaluation using lane stints and a time-overlap overtake proxy."""
    stints_by_track: dict[int, list[dict[str, Any]]] = {}
    for summary in summaries:
        track_id = int(summary["track_id"])
        rows = []
        for stint in summary.get("lane_stints", []):
            enter = float(stint.get("enter_time_s") or 0.0)
            exit_ = float(stint.get("exit_time_s") or enter)
            rows.append(
                {
                    "lane_name": str(stint.get("lane_name", "")),
                    "lane_index": int(stint.get("lane_index") or 0),
                    "enter": enter,
                    "exit": exit_,
                    "dwell": float(stint.get("dwell_seconds") or max(0.0, exit_ - enter)),
                    "prefix": str(stint.get("lane_name", ""))[:2],
                    "hogging": int(summary.get("hogging_event_count", 0)) > 0,
                }
            )
        stints_by_track[track_id] = rows

    # Inner-lane overlap per stint.
    for track_id, rows in stints_by_track.items():
        for row in rows:
            overlap = 0.0
            for other_id, others in stints_by_track.items():
                if other_id == track_id:
                    continue
                for other in others:
                    if other["prefix"] != row["prefix"]:
                        continue
                    if other["lane_index"] != row["lane_index"] - 1:
                        continue
                    overlap += _overlap(
                        (row["enter"], row["exit"]), (other["enter"], other["exit"])
                    )
            row["ratio"] = min(overlap, row["dwell"]) / row["dwell"] if row["dwell"] else 0.0

    predicted_ids: set[int] = set()
    details: dict[int, dict[str, Any]] = {}
    class_results: dict[int, str] = {}
    class_by_track: dict[int, str] = {}
    for summary in summaries:
        track_id = int(summary["track_id"])
        class_by_track[track_id] = str(summary.get("object_class", ""))
    for track_id, rows in stints_by_track.items():
        relevant = [row for row in rows if row["lane_index"] >= 2]
        if not relevant:
            continue
        summary_class = class_by_track.get(track_id, "")
        class_results[track_id] = summary_class
        threshold = (
            params.heavy_dwell if summary_class in HEAVY_CLASSES else params.car_dwell
        )
        details[track_id] = {
            "dwell_seconds": max(row["dwell"] for row in relevant),
            "lane_names": ", ".join(row["lane_name"] for row in relevant),
        }
        for row in relevant:
            if row["dwell"] + 1e-9 < threshold:
                continue
            if params.overtake_exemption and row["ratio"] >= params.overlap_ratio_max:
                continue
            predicted_ids.add(track_id)
            break

    metrics, predictions, excluded = _score(
        labels, predicted_ids, details, observed_ids=set(stints_by_track)
    )
    return Outcome(
        params=params,
        metrics=metrics,
        predictions=predictions,
        mode="tracks",
        notes=["Overtake is approximated from lane-stint time overlap, not bbox position."],
        excluded=excluded,
        class_results=class_results,
    )


# --------------------------------------------------------------------------- search
def _proxy_grid() -> list[Params]:
    grid: list[Params] = []
    for heavy in (0.0, 0.4, 0.6, 1.0, 1.5, 2.0):
        for car in (0.0, 0.4, 0.6, 1.0, 1.5, 2.0):
            for ratio in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
                grid.append(
                    Params(heavy_dwell=heavy, car_dwell=car, overlap_ratio_max=ratio)
                )
    return grid


def _frame_log_grid() -> list[Params]:
    grid: list[Params] = []
    for dwell in (0.4, 0.6):
        for assignment in ("anchor", "bbox_vote"):
            for confirm in (0.0, 0.1, 0.2):
                for margin in (1.0, 3.0, 5.0):
                    for back in (0.0, 0.25, 0.5):
                        grid.append(
                            Params(
                                heavy_dwell=dwell,
                                car_dwell=dwell,
                                assignment=assignment,
                                overtake_exemption=True,
                                overtake_stint_memory=True,
                                overtake_confirm_seconds=confirm,
                                overtake_speed_margin_px_s=margin,
                                overtake_gap_back_fraction=back,
                            )
                        )
    return grid


# --------------------------------------------------------------------------- report
def _rank(outcomes: Sequence[Outcome]) -> list[Outcome]:
    """Rank by F1, then prefer conservative dwell/ratio values on ties."""

    def key(item: Outcome) -> tuple[float, float, float]:
        params = item.params
        # On equal F1, prefer the most stable/threshold-typical values: 0.4 s is
        # the calibrated dwell floor for this camera and 0.8 matches the labels.
        return (
            item.metrics.f1,
            -abs(params.heavy_dwell - 0.4) - abs(params.car_dwell - 0.4),
            -abs(params.overlap_ratio_max - 0.8),
        )

    return sorted(outcomes, key=key, reverse=True)


def _class_accuracy(labels: Sequence[GroundTruthVehicle], outcome: Outcome) -> tuple[int, int, list[str]]:
    correct = 0
    total = 0
    misses: list[str] = []
    for label in labels:
        predicted = ""
        for track_id in label.track_ids:
            if track_id in outcome.class_results:
                predicted = outcome.class_results[track_id]
                break
        if not predicted:
            continue
        total += 1
        if predicted == label.object_class:
            correct += 1
        else:
            misses.append(f"#{label.key}: label={label.object_class} run={predicted}")
    return correct, total, misses


def _write_report(
    out_dir: Path,
    labels: Sequence[GroundTruthVehicle],
    outcomes: Sequence[Outcome],
    baseline: Metrics | None,
    baseline_label: str,
    mode: str,
    excluded: Sequence[str] = (),
) -> Path:
    ensure_dir(out_dir)
    ranked = _rank(outcomes)
    best = ranked[0]
    evaluated = len(labels) - len(excluded)
    lines = [
        "# Lane-hogging rule tuning",
        "",
        f"- Mode: `{mode}`",
        f"- Labels: {len(labels)} vehicles "
        f"({sum(1 for item in labels if item.hogging)} hogger, "
        f"{sum(1 for item in labels if not item.hogging)} overtaking)",
        f"- Evaluated: {evaluated} vehicles; excluded (never observed): "
        f"{', '.join(f'#{key}' for key in excluded) if excluded else 'none'}",
        f"- Parameter sets: {len(outcomes)}",
    ]
    if best.notes:
        lines.append(f"- Note: {' '.join(best.notes)}")
    lines += ["", "## Baseline", ""]
    if baseline is not None:
        lines.append(
            f"{baseline_label}: TP={baseline.tp} FP={baseline.fp} FN={baseline.fn} "
            f"precision={baseline.precision:.3f} recall={baseline.recall:.3f} F1={baseline.f1:.3f}"
        )
    else:
        lines.append("No baseline available for this mode.")
    lines += [
        "",
        "## Best parameter set",
        "",
        "```",
        best.params.label(),
        "```",
        "",
        f"TP={best.metrics.tp} FP={best.metrics.fp} FN={best.metrics.fn} "
        f"precision={best.metrics.precision:.3f} recall={best.metrics.recall:.3f} "
        f"F1={best.metrics.f1:.3f}",
        "",
        "## Top parameter sets",
        "",
        "| # | heavy dwell | car dwell | assignment | overtake | ratio | TP | FP | FN | P | R | F1 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(ranked[:12], start=1):
        params = item.params
        metrics = item.metrics
        lines.append(
            f"| {index} | {params.heavy_dwell:.1f}s | {params.car_dwell:.1f}s | "
            f"{params.assignment} | {'on' if params.overtake_exemption else 'off'} | "
            f"{params.overlap_ratio_max:.2f} | {metrics.tp} | {metrics.fp} | {metrics.fn} | "
            f"{metrics.precision:.3f} | {metrics.recall:.3f} | {metrics.f1:.3f} |"
        )
    correct, total, class_misses = _class_accuracy(labels, best)
    lines += [
        "",
        "## Vehicle type accuracy",
        "",
        f"{correct}/{total} evaluated vehicles classified correctly"
        + (f" ({correct / total:.1%})" if total else "")
        + ".",
    ]
    if class_misses:
        lines += ["", "| Mismatch |", "|---|"]
        lines += [f"| {item} |" for item in class_misses]
    misses = [item for item in best.predictions if item.outcome in {"FP", "FN"}]
    lines += ["", "## Remaining misses", ""]
    if misses:
        lines += [
            "| Vehicle | Outcome | Class | Lane | Max dwell | Stints | Reason |",
            "|---|---|---|---|---|---|---|",
        ]
        for item in misses:
            lines.append(
                f"| #{item.key} | {item.outcome} | {item.object_class} | "
                f"{item.lane_index} | {item.dwell_seconds:.1f}s | {item.lane_names} | {item.note} |"
            )
    else:
        lines.append("None against the labeled vehicles.")
    lines += [
        "",
        "## Notes",
        "",
        "- Labels cover only reviewed vehicles; unlabeled tracks are excluded from scoring.",
        "- Split ids (e.g. `542/565`) count as one vehicle when any id fires.",
        "- Lane hogging here is a rule-based indication, not a legal determination.",
    ]
    report = out_dir / "tuning_report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    (out_dir / "best_params.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "params": best.params.to_dict(),
                "metrics": best.metrics.to_dict(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    with (out_dir / "predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["vehicle_key", "outcome", "predicted", "label", "object_class", "lane_index", "dwell_seconds", "stints", "reason"]
        )
        for item in best.predictions:
            writer.writerow(
                [
                    item.key,
                    item.outcome,
                    item.predicted,
                    item.label,
                    item.object_class,
                    item.lane_index,
                    f"{item.dwell_seconds:.3f}",
                    item.lane_names,
                    item.note,
                ]
            )
    return report


def _write_config(config: AppConfig, params: Params, path: str | Path) -> Path:
    tuned = apply_params(config, params)
    tuned.validate()
    payload = as_plain_dict(tuned)
    target = Path(path)
    ensure_dir(target.parent)
    target.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return target


def run_tuning(
    *,
    config_path: str | Path = "config/config.yaml",
    labels_path: str | Path = "data/labels/lane_hogging_10m.csv",
    frame_log_path: str | Path | None = None,
    tracks_path: str | Path | None = None,
    out_dir: str | Path = "output/tuning",
    apply_path: str | Path | None = None,
    width: int = 1024,
    height: int = 768,
) -> int:
    config = load_config(config_path)
    labels = load_ground_truth(labels_path)
    if not labels:
        raise ValueError(f"No labels found in {labels_path}")
    out = Path(out_dir)
    ensure_dir(out)

    if frame_log_path:
        records = _load_frame_log(frame_log_path)
        if not records:
            raise ValueError(f"Frame log is empty: {frame_log_path}")
        outcomes = [
            evaluate_frame_log(config, records, labels, params, width=width, height=height)
            for params in _frame_log_grid()
        ]
        # Re-evaluate the configured thresholds as a baseline.
        baseline_outcome = evaluate_frame_log(
            config,
            records,
            labels,
            Params(
                heavy_dwell=min(
                    (rule.min_dwell_seconds for rule in config.hogging.rules if "heavy" in rule.name),
                    default=0.4,
                ),
                car_dwell=min(
                    (rule.min_dwell_seconds for rule in config.hogging.rules if "car" in rule.name),
                    default=2.0,
                ),
                assignment=config.occupancy.assignment,
                overtake_exemption=config.hogging.overtake_exemption,
                overtake_gap_fraction=config.hogging.overtake_gap_fraction,
                overtake_gap_back_fraction=config.hogging.overtake_gap_back_fraction,
                overtake_speed_margin_px_s=config.hogging.overtake_speed_margin_px_s,
                overtake_confirm_seconds=config.hogging.overtake_confirm_seconds,
                overtake_stint_memory=config.hogging.overtake_stint_memory,
                evaluate_at=config.hogging.evaluate_at,
            ),
            width=width,
            height=height,
        )
        baseline = baseline_outcome.metrics
        baseline_label = "Current config"
        mode = "frame-log"
    elif tracks_path:
        summaries = load_track_summaries(tracks_path)
        if not summaries:
            raise ValueError(f"Track summaries are empty: {tracks_path}")
        outcomes = [
            evaluate_track_summaries(summaries, labels, params)
            for params in _proxy_grid()
        ]
        predicted_ids = {
            int(item["track_id"])
            for item in summaries
            if int(item.get("hogging_event_count", 0)) > 0
        }
        observed_ids = {int(item["track_id"]) for item in summaries}
        baseline, _, _ = _score(labels, predicted_ids, observed_ids=observed_ids)
        baseline_label = "Run that produced the tracks"
        mode = "tracks"
    else:
        raise ValueError("Pass either --frame-log or --tracks")

    best = _rank(outcomes)[0]
    if mode == "tracks":
        # The proxy cannot evaluate bbox-based assignment; keep the production
        # setting from the base config instead of the grid default.
        best.params.assignment = config.occupancy.assignment
    report = _write_report(
        out,
        labels,
        outcomes,
        baseline,
        baseline_label,
        mode,
        excluded=best.excluded,
    )
    print("Tuning complete")
    print("---------------")
    print(f"Labels       : {len(labels)} vehicles")
    print(f"Evaluated    : {len(outcomes)} parameter set(s)")
    print(
        f"Baseline     : TP={baseline.tp} FP={baseline.fp} FN={baseline.fn} "
        f"F1={baseline.f1:.3f}"
    )
    print(
        f"Best         : TP={best.metrics.tp} FP={best.metrics.fp} FN={best.metrics.fn} "
        f"P={best.metrics.precision:.3f} R={best.metrics.recall:.3f} F1={best.metrics.f1:.3f}"
    )
    print(f"Params       : {best.params.label()}")
    print(f"Report       : {report}")
    if apply_path:
        write_params = best.params
        if mode == "tracks":
            # The proxy does not evaluate overtake/assignment settings; keep the
            # production values from the base config in the written profile.
            write_params = replace(
                best.params,
                assignment=config.occupancy.assignment,
                overtake_exemption=config.hogging.overtake_exemption,
                overtake_gap_fraction=config.hogging.overtake_gap_fraction,
                overtake_gap_back_fraction=config.hogging.overtake_gap_back_fraction,
                overtake_speed_margin_px_s=config.hogging.overtake_speed_margin_px_s,
                overtake_confirm_seconds=config.hogging.overtake_confirm_seconds,
                overtake_stint_memory=config.hogging.overtake_stint_memory,
                evaluate_at=config.hogging.evaluate_at,
            )
        target = _write_config(config, write_params, apply_path)
        print(f"Config       : {target}")
    return 0
