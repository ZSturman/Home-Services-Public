"""Conservative, explainable motion detection for "Out of the House" mode.

Deliberately simple: grayscale frame differencing on a downscaled copy of
each sampled frame, thresholded per-pixel, with a minimum-changed-area
fraction and a minimum-consecutive-frames requirement before anything is
considered "motion". This Pi has no GPU worth exploiting for a heavier model,
and a tunable, inspectable detector is what a remote arm/disarm camera
feature actually needs -- see docs/camera.md#motion-detection.

This module owns the pure detection math and the on-disk event record
format. Device/ffmpeg orchestration (when to sample, when to record) lives
in camera_service.py, which is easier to test end-to-end with recording
mocked out than to tangle in here.
"""
from __future__ import annotations

import io
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import MotionConfig

EVENT_META_FILENAME = "meta.json"
RECORDING_FILENAME = "recording.mp4"


class FrameDecodeError(RuntimeError):
    pass


def decode_grayscale(image_bytes: bytes, size: tuple[int, int]):
    """Decode a JPEG to a downscaled grayscale array for cheap diffing."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise FrameDecodeError("Pillow/numpy are required for motion detection") from exc
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            im = im.convert("L").resize(size)
            return np.asarray(im, dtype=np.int16)
    except Exception as exc:
        raise FrameDecodeError(f"could not decode frame: {exc}") from exc


def changed_fraction(prev, curr, sensitivity: int) -> float:
    """Fraction (0..1) of pixels whose grayscale value changed by more than
    `sensitivity`. `prev`/`curr` are equal-shaped arrays from decode_grayscale."""
    import numpy as np
    diff = np.abs(curr.astype(np.int16) - prev.astype(np.int16))
    changed = diff > sensitivity
    return float(changed.sum()) / float(changed.size)


@dataclass
class MotionDetector:
    """Stateful per-camera detector: feed it frames in order via `observe`.

    Requires `consecutive_frames` in a row over `min_changed_area_fraction`
    before reporting motion, and stays silent again after `cooldown_seconds`
    without a fresh over-threshold frame (small, incidental noise like a
    single compression artifact or an auto-exposure flicker frame should
    never register as motion).
    """
    config: MotionConfig
    _prev_gray: object = field(default=None, repr=False)
    _consecutive: int = 0

    def reset(self) -> None:
        self._prev_gray = None
        self._consecutive = 0

    def observe(self, image_bytes: bytes) -> bool:
        """Returns True the instant this frame satisfies the
        consecutive-frames-over-threshold rule (i.e. once per rising edge --
        callers should track their own event/cooldown state from there)."""
        size = (self.config.analysis_width, self.config.analysis_height)
        gray = decode_grayscale(image_bytes, size)
        if self._prev_gray is None:
            self._prev_gray = gray
            self._consecutive = 0
            return False
        fraction = changed_fraction(self._prev_gray, gray, self.config.sensitivity)
        self._prev_gray = gray
        if fraction >= self.config.min_changed_area_fraction:
            self._consecutive += 1
        else:
            self._consecutive = 0
        return self._consecutive >= self.config.consecutive_frames


def new_event_id(when: float | None = None) -> str:
    ts = when if when is not None else time.time()
    return f"{time.strftime('%Y%m%dT%H%M%S', time.localtime(ts))}-{uuid.uuid4().hex[:6]}"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, indent=2))
    os.replace(temp, path)


def create_event(events_root: Path, *, event_id: str, start_ts: float, camera_device_name: str,
                  pre_context: list[str] | None = None) -> Path:
    event_dir = events_root / event_id
    event_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "event_id": event_id,
        "start_ts": start_ts,
        "last_motion_ts": start_ts,
        "end_ts": None,
        "camera_device_name": camera_device_name,
        "recording_started": False,
        "recording_path": None,
        "pre_context": pre_context or [],
        "synced": False,
        "notified": False,
    }
    _atomic_write_json(event_dir / EVENT_META_FILENAME, meta)
    return event_dir


def read_event_meta(event_dir: Path) -> dict:
    return json.loads((event_dir / EVENT_META_FILENAME).read_text())


def update_event_meta(event_dir: Path, **fields) -> dict:
    meta = read_event_meta(event_dir)
    meta.update(fields)
    _atomic_write_json(event_dir / EVENT_META_FILENAME, meta)
    return meta


def list_events(events_root: Path, limit: int = 50) -> list[dict]:
    """Most recent events first. Cheap: reads only meta.json per event, never
    the (potentially large) recording file."""
    if not events_root.exists():
        return []
    events = []
    for event_dir in events_root.iterdir():
        if not event_dir.is_dir():
            continue
        try:
            meta = read_event_meta(event_dir)
        except (OSError, ValueError):
            continue
        events.append(meta)
    events.sort(key=lambda m: m.get("start_ts") or 0, reverse=True)
    return events[:limit]
