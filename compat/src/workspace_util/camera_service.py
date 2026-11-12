"""Server-side camera orchestration: periodic snapshot capture, motion
sampling, motion-triggered recording, and retention -- running in one
background thread inside the dashboard process, independent of any browser
page being open. See docs/camera.md#service-architecture.

Exactly one CameraService instance is created per dashboard process (see
dashboard/app.py's startup hook); its own locks serialize all device access,
including the on-demand snapshot API route, so the periodic loop, motion
sampling, motion recording, and manual "view camera" requests can never open
the V4L2 device at the same time.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from . import camera as camera_mod
from . import motion as motion_mod
from .config import Config
from .logs import get_logger
from .notify import Notifier, SUBJECTS, build_notifier

STATE_FILENAME = "camera_state.json"
RETENTION_INTERVAL_SECONDS = 3600
_TICK_SECONDS = 1.0
_PRE_CONTEXT_FRAMES = 6


def state_path(logging_dir: str) -> Path:
    return Path(logging_dir) / STATE_FILENAME


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True))
    os.replace(temp, path)


def load_out_of_house(logging_dir: str, default: bool) -> bool:
    try:
        return bool(json.loads(state_path(logging_dir).read_text()).get("out_of_house", default))
    except (OSError, ValueError):
        return default


def save_out_of_house(logging_dir: str, armed: bool) -> None:
    _atomic_write_json(state_path(logging_dir), {"out_of_house": armed, "updated_at": time.time()})


class CameraService:
    def __init__(self, cfg: Config, notifier: Notifier | None = None):
        self.cfg = cfg
        self.camera_cfg = cfg.camera
        self.logger = get_logger("camera", cfg.logging.dir)
        self.motion_logger = get_logger("motion", cfg.logging.dir)
        self.notifier = notifier or build_notifier(cfg.notifications, cfg.logging.dir)
        self.detector = motion_mod.MotionDetector(cfg.camera.motion)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._device_lock = threading.Lock()
        self._state_lock = threading.RLock()

        self.armed = load_out_of_house(cfg.logging.dir, cfg.camera.out_of_house_default)
        self.recording_process: subprocess.Popen | None = None
        self.current_event: dict | None = None
        self.recent_frames: deque = deque(maxlen=_PRE_CONTEXT_FRAMES)

        self.last_snapshot_at: float | None = None
        self.last_snapshot_path: str | None = None
        self.last_sample_at: float | None = None
        self.last_capture_error: str | None = None
        self.consecutive_failures = 0
        self.last_motion_at: float | None = None
        self._last_retention_sweep = 0.0

    # -- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self.logger.warning("camera service start() called while already running -- ignoring")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera-service", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._state_lock:
            recording = self.current_event is not None
        if recording:
            self._finalize_event(time.time())
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                self.logger.exception("camera service tick failed")
            self._stop.wait(_TICK_SECONDS)

    # -- scheduling ----------------------------------------------------
    def _tick(self) -> None:
        cfg = self.camera_cfg
        if not cfg.enabled:
            return
        now = time.time()
        with self._state_lock:
            recording = self.current_event is not None
        if recording:
            self._service_recording(now)
        elif self.armed:
            self._service_armed_idle(now)
        else:
            self._service_unarmed(now)

        if now - self._last_retention_sweep > RETENTION_INTERVAL_SECONDS:
            self._last_retention_sweep = now
            self._retention_sweep()

    def _service_unarmed(self, now: float) -> None:
        interval = max(3, self.camera_cfg.refresh_seconds)
        if self.last_snapshot_at is not None and now - self.last_snapshot_at < interval:
            return
        image = self._grab_frame()
        if image is not None:
            self._save_frame(image, now)

    def _service_armed_idle(self, now: float) -> None:
        sample_interval = max(0.5, self.camera_cfg.motion.sample_interval_seconds)
        if self.last_sample_at is not None and now - self.last_sample_at < sample_interval:
            return
        image = self._grab_frame()
        self.last_sample_at = now
        if image is None:
            return
        self.recent_frames.append((now, image))

        interval = max(3, self.camera_cfg.refresh_seconds)
        if self.last_snapshot_at is None or now - self.last_snapshot_at >= interval:
            self._save_frame(image, now)

        try:
            triggered = self.detector.observe(image)
        except motion_mod.FrameDecodeError as exc:
            self.motion_logger.warning(f"could not decode frame for motion analysis: {exc}")
            return
        if triggered:
            self._start_event(now)

    # -- device access (always through _device_lock) --------------------
    def _grab_frame(self) -> bytes | None:
        with self._device_lock:
            try:
                image = camera_mod.capture_snapshot(self.camera_cfg)
            except camera_mod.CameraCaptureError as exc:
                self.consecutive_failures += 1
                self.last_capture_error = str(exc)
                self.logger.warning(f"camera capture failed ({self.consecutive_failures}x): {exc}")
                if self.consecutive_failures == 3:
                    self.notifier.notify("warning", SUBJECTS["camera_unavailable"], str(exc))
                return None
        self.consecutive_failures = 0
        self.last_capture_error = None
        return image

    def capture_on_demand(self) -> bytes:
        """The synchronous, user-facing "view camera now" path -- never
        blocks: reports CameraBusyError immediately if the background loop
        or a motion recording currently owns the device."""
        with self._state_lock:
            if self.current_event is not None:
                raise camera_mod.CameraBusyError("camera is currently recording a motion event")
        if not self._device_lock.acquire(blocking=False):
            raise camera_mod.CameraBusyError("camera is already capturing a frame")
        try:
            return camera_mod.capture_snapshot(self.camera_cfg)
        finally:
            self._device_lock.release()

    def _save_frame(self, image: bytes, now: float) -> None:
        try:
            path = camera_mod.save_snapshot(self.camera_cfg, image, when=now)
        except camera_mod.CameraStorageUnavailableError as exc:
            self.last_capture_error = str(exc)
            self.logger.warning(f"camera storage unavailable: {exc}")
            return
        self.last_snapshot_at = now
        self.last_snapshot_path = str(path)

    # -- motion events / recording ---------------------------------------
    def _start_event(self, now: float) -> None:
        with self._state_lock:
            if self.current_event is not None:
                # Defense in depth: _tick() already routes to _service_recording
                # once an event is active, so this should be unreachable in
                # normal operation -- but a motion event must never spawn a
                # second overlapping recording no matter how it's invoked.
                return
        cfg = self.camera_cfg
        event_id = motion_mod.new_event_id(now)
        try:
            root = camera_mod.ensure_storage_dir(cfg)
        except camera_mod.CameraStorageUnavailableError as exc:
            self.motion_logger.warning(f"cannot start motion event, storage unavailable: {exc}")
            return
        events_root = root / camera_mod.EVENTS_SUBDIR
        event_dir = motion_mod.create_event(
            events_root, event_id=event_id, start_ts=now, camera_device_name=cfg.device_name,
        )
        pre_context = []
        for i, (_, frame) in enumerate(self.recent_frames):
            name = f"pre-{i:02d}.jpg"
            try:
                (event_dir / name).write_bytes(frame)
                pre_context.append(name)
            except OSError:
                pass
        recording_ok = self._start_recording(event_dir, cfg)
        motion_mod.update_event_meta(
            event_dir, pre_context=pre_context, recording_started=recording_ok,
            recording_path=motion_mod.RECORDING_FILENAME if recording_ok else None,
        )
        with self._state_lock:
            self.current_event = {"id": event_id, "dir": event_dir, "started_at": now}
        self.last_motion_at = now
        self.motion_logger.warning(f"motion event {event_id} started (recording={recording_ok})")
        self._notify_motion(event_id, recording_ok)

    def _start_recording(self, event_dir: Path, cfg) -> bool:
        recording_path = event_dir / motion_mod.RECORDING_FILENAME
        live_frame_path = event_dir / "live_frame.jpg"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", f"{cfg.width}x{cfg.height}",
            "-framerate", str(max(1, cfg.motion.recording_fps)),
            "-i", cfg.device,
            "-t", str(max(5, cfg.motion.max_recording_seconds)),
            "-map", "0:v", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(recording_path),
            "-map", "0:v", "-vf", "fps=1", "-update", "1", str(live_frame_path),
        ]
        with self._device_lock:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except OSError as exc:
                self.logger.error(f"failed to start recording: {exc}")
                return False
        time.sleep(0.5)  # ffmpeg exits almost immediately if the device is busy/missing
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode("utf-8", errors="replace").strip() if proc.stderr else ""
            self.logger.error(f"recording process exited immediately: {stderr}")
            return False
        self.recording_process = proc
        return True

    def _service_recording(self, now: float) -> None:
        with self._state_lock:
            event = self.current_event
        if event is None:
            return
        proc = self.recording_process
        if proc is None or proc.poll() is not None:
            self._finalize_event(now)
            return

        sample_interval = max(0.5, self.camera_cfg.motion.sample_interval_seconds)
        if self.last_sample_at is None or now - self.last_sample_at >= sample_interval:
            self.last_sample_at = now
            live_frame_path = event["dir"] / "live_frame.jpg"
            if live_frame_path.exists():
                try:
                    image = live_frame_path.read_bytes()
                    if image and self.detector.observe(image):
                        self.last_motion_at = now
                        motion_mod.update_event_meta(event["dir"], last_motion_ts=now)
                except (OSError, motion_mod.FrameDecodeError):
                    pass

        quiet_for = now - (self.last_motion_at or event["started_at"])
        elapsed = now - event["started_at"]
        if quiet_for >= self.camera_cfg.motion.recording_seconds_after_motion or \
                elapsed >= self.camera_cfg.motion.max_recording_seconds:
            self._finalize_event(now)

    def _stop_recording_process(self) -> None:
        proc = self.recording_process
        self.recording_process = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _finalize_event(self, now: float) -> None:
        with self._state_lock:
            event = self.current_event
            self.current_event = None
        if event is None:
            return
        self._stop_recording_process()
        try:
            (event["dir"] / "live_frame.jpg").unlink(missing_ok=True)
        except OSError:
            pass
        recording_path = event["dir"] / motion_mod.RECORDING_FILENAME
        recording_ok = recording_path.exists() and recording_path.stat().st_size > 0
        motion_mod.update_event_meta(
            event["dir"], end_ts=now, recording_started=recording_ok,
            recording_path=motion_mod.RECORDING_FILENAME if recording_ok else None,
        )
        self.motion_logger.info(f"motion event {event['id']} closed")
        self.detector.reset()

    def _notify_motion(self, event_id: str, recording_ok: bool) -> None:
        when = time.strftime("%Y-%m-%d %H:%M:%S")
        body = (
            f"Motion detected on {self.camera_cfg.device_name} at {when}. "
            f"Recording {'started' if recording_ok else 'could not start'}."
        )
        external_url = (self.cfg.dashboard.external_url or "").rstrip("/")
        if external_url:
            body += f" View: {external_url}/#/camera/events/{event_id}"
        self.notifier.notify("warning", SUBJECTS["motion_detected"], body)

    # -- retention / sync ------------------------------------------------
    def _retention_sweep(self) -> None:
        try:
            camera_mod.cleanup_snapshots(self.camera_cfg)
        except Exception:
            self.logger.exception("snapshot retention sweep failed")
        try:
            self._mark_synced_if_caught_up()
        except Exception:
            self.logger.exception("sync status check failed")
        try:
            camera_mod.cleanup_events(self.camera_cfg)
        except Exception:
            self.logger.exception("event retention sweep failed")

    def _mark_synced_if_caught_up(self) -> None:
        if not self.camera_cfg.sync_enabled:
            return
        from . import camera_sync
        status = camera_sync.folder_sync_status(self.cfg)
        if not status.get("configured") or status.get("need_files", 1) != 0:
            return
        events_root = Path(self.camera_cfg.storage_dir) / camera_mod.EVENTS_SUBDIR
        if not events_root.exists():
            return
        now = time.time()
        for event_dir in events_root.iterdir():
            if not event_dir.is_dir():
                continue
            try:
                meta = motion_mod.read_event_meta(event_dir)
            except (OSError, ValueError):
                continue
            end_ts = meta.get("end_ts")
            if meta.get("synced") or end_ts is None:
                continue
            if now - end_ts < 60:
                continue  # grace period for Syncthing's own scan debounce
            motion_mod.update_event_meta(event_dir, synced=True)

    # -- external control / status ---------------------------------------
    def arm(self, value: bool) -> None:
        with self._state_lock:
            self.armed = value
        save_out_of_house(self.cfg.logging.dir, value)
        self.detector.reset()
        self.motion_logger.warning(f"Out of the House mode {'armed' if value else 'disarmed'}")

    def status_dict(self) -> dict:
        with self._state_lock:
            recording = self.current_event is not None
            event_id = self.current_event["id"] if self.current_event else None
        base = camera_mod.status(self.camera_cfg)
        base.update({
            "out_of_house": self.armed,
            "recording": recording,
            "current_event_id": event_id,
            "last_snapshot_at": self.last_snapshot_at,
            "last_capture_error": self.last_capture_error,
            "consecutive_failures": self.consecutive_failures,
            "last_motion_at": self.last_motion_at,
        })
        return base
