"""Vehicle detection and tracking backends.

The default backend wraps Ultralytics YOLO with ByteTrack. The import is lazy so
configuration, analytics, and the offline demo still work without PyTorch.
"""

from __future__ import annotations

from typing import Protocol, Sequence

import numpy as np

from .models import TrackedObject


class Detector(Protocol):
    """Frame-by-frame detection/tracking interface used by the pipeline."""

    name: str

    def update(self, frame: np.ndarray, frame_index: int) -> list[TrackedObject]:
        ...


class YoloByteTrackDetector:
    """Pretrained YOLO detector + ByteTrack tracker, CPU or GPU."""

    def __init__(
        self,
        weights: str = "yolo11n.pt",
        *,
        device: str = "cpu",
        imgsz: int = 640,
        conf: float = 0.35,
        iou: float = 0.5,
        classes: Sequence[int] = (2, 5, 7),
        tracker: str = "bytetrack.yaml",
    ) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "ultralytics is not installed. Install the full runtime with "
                "'pip install -r requirements.txt' (CPU PyTorch wheels first)."
            ) from exc

        self._model = YOLO(weights)
        self._device = device
        self._imgsz = imgsz
        self._conf = conf
        self._iou = iou
        self._classes = list(classes)
        self._tracker = tracker
        self._names = getattr(self._model, "names", {})
        self.name = f"yolo+bytetrack:{weights}"

    def reset(self) -> None:
        """Drop the ByteTrack state so the model can be reused for a new segment.

        The YOLO weights are expensive to load but the tracker is not; parallel
        segment workers keep one model alive per process and reset only the
        tracker between segments.
        """
        predictor = getattr(self._model, "predictor", None)
        if predictor is None:
            return
        try:
            predictor.trackers = []
        except Exception:  # pragma: no cover - defensive against API changes
            pass

    def _class_name(self, class_id: int) -> str:
        if isinstance(self._names, dict):
            return str(self._names.get(class_id, class_id))
        try:
            return str(self._names[class_id])
        except (IndexError, TypeError):
            return str(class_id)

    def update(self, frame: np.ndarray, frame_index: int) -> list[TrackedObject]:
        results = self._model.track(
            source=frame,
            persist=True,
            tracker=self._tracker,
            conf=self._conf,
            iou=self._iou,
            imgsz=self._imgsz,
            classes=self._classes,
            device=self._device,
            verbose=False,
        )
        if not results:
            return []

        boxes = getattr(results[0], "boxes", None)
        if boxes is None or len(boxes) == 0 or getattr(boxes, "id", None) is None:
            return []

        track_ids = boxes.id.int().cpu().tolist()
        xyxy = boxes.xyxy.cpu().numpy()
        class_ids = boxes.cls.int().cpu().tolist()
        confidences = boxes.conf.cpu().tolist()

        objects: list[TrackedObject] = []
        for track_id, box, class_id, confidence in zip(track_ids, xyxy, class_ids, confidences):
            objects.append(
                TrackedObject(
                    track_id=int(track_id),
                    class_id=int(class_id),
                    class_name=self._class_name(int(class_id)),
                    confidence=float(confidence),
                    bbox=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                )
            )
        return objects
