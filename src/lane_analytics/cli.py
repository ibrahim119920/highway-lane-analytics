"""Command line interface: analyze, preview-lanes, summarize, demo, record, doctor."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import os
import platform
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .config import ConfigError, describe_config, load_config
from .utils import ensure_dir, format_counts, human_duration, setup_logging

if TYPE_CHECKING:
    from .pipeline import RunResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lane-analytics",
        description="Detect, track, and analyze highway lane occupancy and lane hogging.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="process a traffic video with YOLO + ByteTrack")
    analyze.add_argument("--config", default="config/config.yaml", help="path to the YAML config")
    analyze.add_argument("--video", help="override video.path")
    analyze.add_argument("--output-video", help="override the annotated video path")
    analyze.add_argument("--events", help="override the JSONL events path")
    analyze.add_argument("--tracks", help="override the JSONL track-summary path")
    analyze.add_argument("--reports-dir", help="override the reports directory")
    analyze.add_argument("--run-id", help="stable run identifier used in output filenames")
    analyze.add_argument("--camera-id", help="override camera_id")
    analyze.add_argument("--base-timestamp", help="override base_timestamp (ISO-8601)")
    analyze.add_argument("--frame-stride", type=int, help="process every Nth frame")
    analyze.add_argument("--max-frames", type=int, help="stop after N source frames")
    analyze.add_argument(
        "--start-frame", type=int, help="first source frame to process (seek before decoding)"
    )
    analyze.add_argument("--model", help="override model.weights (e.g. yolov8n.pt)")
    analyze.add_argument("--device", help="cpu or GPU index, e.g. 0")
    analyze.add_argument("--imgsz", type=int, help="inference size")
    analyze.add_argument("--conf", type=float, help="detection confidence threshold")
    analyze.add_argument("--no-video", action="store_true", help="skip annotated video rendering")
    analyze.add_argument("--no-reports", action="store_true", help="skip CSV/JSON/Markdown reports")

    preview = subparsers.add_parser(
        "preview-lanes", help="draw the configured lane polygons on one video frame"
    )
    preview.add_argument("--config", default="config/config.yaml")
    preview.add_argument("--video", help="override video.path")
    preview.add_argument("--frame", type=int, default=0, help="source frame index to render")
    preview.add_argument("--out", default="output/lane_preview.jpg")

    calibrate = subparsers.add_parser(
        "calibrate",
        help="render one frame with a labelled pixel/normalized grid for drawing lane polygons",
    )
    calibrate.add_argument("--config", default="config/config.yaml")
    calibrate.add_argument("--video", help="override video.path")
    calibrate.add_argument("--frame", type=int, default=0, help="source frame index to render")
    calibrate.add_argument("--out", default="output/calibration/grid.jpg")
    calibrate.add_argument("--step", type=int, default=50, help="grid spacing in pixels")
    calibrate.add_argument(
        "--no-polygons", action="store_true", help="hide the currently configured polygons"
    )

    summarize = subparsers.add_parser("summarize", help="build reports from existing JSONL files")
    summarize.add_argument("events", help="path to an events JSONL or CSV file")
    summarize.add_argument("--tracks", help="path to a tracks JSONL file (optional)")
    summarize.add_argument("--out", default="output/reports/summary", help="output directory")
    summarize.add_argument("--interval", type=int, default=60, help="time bucket in seconds")
    summarize.add_argument("--camera-id", help="override the camera id from the events")

    demo = subparsers.add_parser(
        "demo", help="generate a synthetic multi-lane scene and run the whole pipeline offline"
    )
    demo.add_argument("--output-dir", default="output/demo")
    demo.add_argument("--duration", type=float, default=20.0, help="seconds of synthetic video")
    demo.add_argument("--fps", type=int, default=10)
    demo.add_argument("--width", type=int, default=640)
    demo.add_argument("--height", type=int, default=640)
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument("--frame-stride", type=int, default=1)
    demo.add_argument("--preview-frame", type=int, default=120)

    record = subparsers.add_parser(
        "record", help="record a live HLS/HTTP stream for a fixed duration"
    )
    record.add_argument("--url", required=True, help="HLS playlist or direct stream URL")
    record.add_argument("--out", required=True, help="output video path, e.g. data/raw/clip.mp4")
    record.add_argument("--duration", type=float, required=True, help="seconds to record")
    record.add_argument(
        "--chunk-seconds",
        type=float,
        default=30.0,
        help="length of each reconnect slice (default: 30)",
    )
    record.add_argument(
        "--max-attempts", type=int, default=30, help="cap on chunk attempts (default: 30)"
    )
    record.add_argument(
        "--stall-timeout",
        type=float,
        default=20.0,
        help="seconds without data before a chunk is retried (default: 20)",
    )
    record.add_argument(
        "--no-reencode-fallback",
        action="store_true",
        help="only stream-copy; fail instead of re-encoding when chunks do not mux",
    )

    subparsers.add_parser("doctor", help="check the local environment and dependencies")
    return parser


def _apply_overrides(config, args: argparse.Namespace):
    if args.video:
        config.video.path = args.video
    if args.frame_stride is not None:
        config.video.frame_stride = args.frame_stride
    if args.max_frames is not None:
        config.video.max_frames = args.max_frames
    if args.camera_id:
        config.camera_id = args.camera_id
    if args.base_timestamp:
        config.base_timestamp = args.base_timestamp
    if args.model:
        config.model.weights = args.model
    if args.device:
        config.model.device = args.device
    if args.imgsz is not None:
        config.model.imgsz = args.imgsz
    if args.conf is not None:
        config.model.conf = args.conf
    config.validate()
    return config


def _print_run_summary(result: "RunResult") -> None:
    summary = result.summary
    print()
    print("Lane occupancy summary")
    print("----------------------")
    print(f"Run ID       : {result.run_id}")
    print(f"Camera       : {result.camera_id}")
    print(f"Vehicles     : {result.total_vehicles}")
    print(f"By class     : {format_counts(summary.get('vehicles_by_class') or {})}")
    print(f"Events       : {result.total_events}")
    print(f"By type      : {format_counts(summary.get('events_by_type') or {})}")
    print(f"Lane changes : {int((summary.get('lane_changes') or {}).get('total') or 0)}")
    print(f"Hogging      : {result.hogging_events} indication(s)")
    by_severity = (summary.get("hogging") or {}).get("by_severity") or {}
    if by_severity:
        print(f"Severity     : {format_counts(by_severity)}")
    peak = summary.get("peak_interval")
    if peak:
        print(f"Peak interval: {peak['interval_start']} ({peak['count']} events)")
    print(
        "Processed    : "
        f"{result.frames_processed} frames in {human_duration(result.processing_s)} "
        f"({result.processing_fps:.1f} fps)"
    )
    if result.output_video:
        print(f"Video        : {result.output_video}")
    else:
        print("Video        : disabled")
    print(f"Events file  : {result.events_path}")
    print(f"Tracks file  : {result.tracks_path}")
    if result.reports_dir:
        print(f"Reports      : {result.reports_dir}")
    print()
    print("Note: lane hogging is a rule-based indication, not a legal determination.")


def _read_source_frame(video_path: str | Path, frame_index: int):
    import cv2

    source = Path(video_path)
    if not source.exists():
        raise FileNotFoundError(f"Video not found: {source}")

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV could not open video: {source}")
    try:
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        frame_index = max(0, frame_index)
        if frame_count > 0:
            frame_index = min(frame_index, frame_count - 1)
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok or frame is None:
            raise RuntimeError(f"Could not read frame {frame_index} from {source}")
    finally:
        capture.release()
    return frame, frame_index


def _draw_calibration_grid(frame, step: int = 50):
    """Overlay a labelled pixel grid plus the normalized (0-1) every-0.1 ruler."""
    import cv2

    height, width = frame.shape[:2]
    step = max(10, int(step))
    for x in range(0, width, step):
        major = (x % (step * 2)) == 0
        cv2.line(frame, (x, 0), (x, height), (0, 120, 255) if major else (70, 70, 70), 1)
        if major:
            cv2.putText(
                frame, f"{x}", (x + 2, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.34, (0, 200, 255), 1, cv2.LINE_AA
            )
    for y in range(0, height, step):
        major = (y % (step * 2)) == 0
        cv2.line(frame, (0, y), (width, y), (0, 120, 255) if major else (70, 70, 70), 1)
        if major:
            cv2.putText(
                frame, f"{y}", (2, y - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.34, (0, 200, 255), 1, cv2.LINE_AA
            )
            cv2.putText(
                frame,
                f"{y / height:.2f}",
                (width - 44, y - 3),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.34,
                (0, 200, 255),
                1,
                cv2.LINE_AA,
            )
    return frame


def _calibrate(args: argparse.Namespace) -> int:
    import cv2

    from .annotator import Annotator
    from .pipeline import build_lane_regions

    config = load_config(args.config)
    if args.video:
        config.video.path = args.video
    config.validate()

    frame, frame_index = _read_source_frame(config.video.path, args.frame)
    if not args.no_polygons:
        annotator = Annotator(build_lane_regions(config), config.camera_id)
        annotator.draw_lanes(frame)
    _draw_calibration_grid(frame, args.step)

    target = Path(args.out)
    ensure_dir(target.parent)
    cv2.imwrite(str(target), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    height, width = frame.shape[:2]
    print(f"Wrote {target} (frame {frame_index}, {width}x{height})")
    print("Grid: x/y ticks are source pixels; right-edge labels are normalized y.")
    print("Read a lane edge at two heights, then divide x by width and y by height.")
    print()
    for lane in config.lanes:
        coordinates = " -> ".join(f"({x:.4f}, {y:.4f})" for x, y in lane.polygon)
        print(f"  {lane.name}: {coordinates}")
    return 0


def _preview_lanes(args: argparse.Namespace) -> int:
    import cv2

    from .annotator import Annotator
    from .pipeline import build_lane_regions

    config = load_config(args.config)
    if args.video:
        config.video.path = args.video
    config.validate()

    frame, frame_index = _read_source_frame(config.video.path, args.frame)

    lanes = build_lane_regions(config)
    annotator = Annotator(lanes, config.camera_id)
    annotator.draw_lanes(frame)

    target = Path(args.out)
    ensure_dir(target.parent)
    cv2.imwrite(str(target), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    print(f"Wrote {target} (frame {frame_index})")
    for lane in lanes:
        coordinates = " -> ".join(f"({x:.3f}, {y:.3f})" for x, y in lane.points)
        print(
            f"  {lane.name}: index={lane.index if lane.index is not None else '-'} "
            f"role={lane.role} polygon={coordinates}"
        )
    return 0


def _record(args: argparse.Namespace) -> int:
    from .recorder import record_stream

    result = record_stream(
        args.url,
        args.out,
        args.duration,
        chunk_seconds=args.chunk_seconds,
        max_attempts=args.max_attempts,
        stall_timeout=args.stall_timeout,
        reencode_fallback=not args.no_reencode_fallback,
    )
    print()
    print(f"Recorded     : {human_duration(result.actual_seconds)} of {human_duration(args.duration)}")
    print(f"Chunks       : {result.chunk_count} ({result.attempts} attempt(s))")
    print(f"Codec        : {'re-encoded' if result.reencoded else 'stream copy'}")
    print(f"Video        : {result.output}")
    if not result.complete:
        print(
            f"warning: short by {human_duration(result.shortfall_seconds)} - the live stream "
            "stopped early. Re-run the command to top up the clip.",
            file=sys.stderr,
        )
        return 1
    return 0


def _doctor() -> int:
    print(f"highway-lane-occupancy {__version__}")
    print(f"Python      : {platform.python_version()} ({platform.platform()})")
    print(f"CPU cores   : {os.cpu_count()}")
    print()
    packages = [
        ("opencv-python", "cv2", True),
        ("numpy", "numpy", True),
        ("pandas", "pandas", True),
        ("PyYAML", "yaml", True),
        ("ultralytics", "ultralytics", False),
        ("torch", "torch", False),
    ]
    missing_demo = []
    missing_full = []
    for distribution, module, required_for_demo in packages:
        if importlib.util.find_spec(module) is None:
            status = "MISSING"
            (missing_demo if required_for_demo else missing_full).append(distribution)
        else:
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                version = "installed"
            status = f"ok ({version})"
        print(f"{distribution:<12}: {status}")

    print()
    if missing_full:
        print(
            "Real video analysis needs: "
            + ", ".join(missing_full)
            + ". Install with 'pip install -e .'."
        )
    else:
        print("Full YOLO + ByteTrack runtime looks ready.")
    if missing_demo:
        print("The offline demo still needs: " + ", ".join(missing_demo))
    else:
        print("Offline demo dependencies are ready: 'lane-analytics demo'")
    return 0 if not missing_demo else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    try:
        if args.command == "analyze":
            from .pipeline import process_video

            config = load_config(args.config)
            config = _apply_overrides(config, args)
            print(f"Using config: {Path(args.config)}")
            print(describe_config(config))
            print()
            result = process_video(
                config,
                output_video=args.output_video,
                events_path=args.events,
                tracks_path=args.tracks,
                reports_dir=args.reports_dir,
                write_video=not args.no_video,
                generate_reports=not args.no_reports,
                run_id=args.run_id,
                start_frame=args.start_frame or 0,
            )
            _print_run_summary(result)
            return 0

        if args.command == "preview-lanes":
            return _preview_lanes(args)

        if args.command == "calibrate":
            return _calibrate(args)

        if args.command == "summarize":
            from .analytics import load_events, load_tracks, summarize_events, write_reports

            events = load_events(args.events)
            tracks = load_tracks(args.tracks)
            summary = summarize_events(
                events,
                tracks,
                interval_seconds=args.interval,
                camera_id=args.camera_id,
            )
            paths = write_reports(events, tracks, summary, args.out, interval_seconds=args.interval)
            print(f"Loaded {len(events)} events and {len(tracks)} tracks")
            print(f"Total events: {summary['total_events']}")
            print(f"By type     : {format_counts(summary.get('events_by_type') or {})}")
            print(f"Hogging     : {int((summary.get('hogging') or {}).get('total') or 0)}")
            for name, path in paths.items():
                print(f"{name:<20}: {path}")
            return 0

        if args.command == "demo":
            from .demo import run_demo

            demo = run_demo(
                args.output_dir,
                duration=args.duration,
                fps=args.fps,
                width=args.width,
                height=args.height,
                seed=args.seed,
                frame_stride=args.frame_stride,
                preview_frame=args.preview_frame,
            )
            _print_run_summary(demo.run)
            print(f"Input video  : {demo.input_video}")
            print(f"Manifest     : {demo.manifest_path}")
            if demo.preview_path:
                print(f"Preview      : {demo.preview_path}")
            print(f"Scene vehicles: {demo.vehicle_count} generated")
            print()
            print("Note: this scene is synthetic. Use 'analyze' with real footage for YOLO detection.")
            return 0

        if args.command == "record":
            return _record(args)

        if args.command == "doctor":
            return _doctor()

        parser.error(f"unknown command: {args.command}")
        return 2
    except (ConfigError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
