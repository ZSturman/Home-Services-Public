"""Low-rate V4L2 camera capture for the private dashboard, plus persistent,
retention-managed snapshot storage.

The project deliberately does not run a second streaming server or publish a
camera port.  Every capture (on-demand or the periodic background loop)
grabs one local device frame; the dashboard's existing Tailnet and token
boundary protects every resulting image, snapshot, or recording -- see
docs/camera.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import CameraConfig
from .paths import PathNotAllowedError, join_within_root

SNAPSHOTS_SUBDIR = "snapshots"
EVENTS_SUBDIR = "events"
_JPEG_MAGIC = b"\xff\xd8"


class CameraCaptureError(RuntimeError):
    pass


class CameraBusyError(CameraCaptureError):
    """The device is already in use (another capture in flight, or a motion
    recording is active). Distinct from CameraCaptureError so callers can
    map it to 429 instead of 503."""


class CameraStorageUnavailableError(RuntimeError):
    """Raised when storage_dir cannot be safely used yet (e.g. the backup HDD
    it lives on is not mounted). Never falls back to writing under the code
    checkout or the OS/root filesystem -- see mount_safety.py's rationale,
    applied here to a directory rather than a mount point."""


def status(camera: CameraConfig) -> dict:
    device = Path(camera.device)
    ffmpeg = shutil.which("ffmpeg")
    available = camera.enabled and device.exists() and ffmpeg is not None
    if not camera.enabled:
        detail = "camera disabled in config"
    elif not device.exists():
        detail = f"camera device not found: {device}"
    elif ffmpeg is None:
        detail = "ffmpeg is not installed"
    else:
        detail = "ready"
    return {
        "enabled": camera.enabled,
        "available": available,
        "detail": detail,
        "device": camera.device,
        "width": camera.width,
        "height": camera.height,
        "refresh_seconds": max(3, camera.refresh_seconds),
    }


def capture_snapshot(camera: CameraConfig) -> bytes:
    current = status(camera)
    if not current["available"]:
        raise CameraCaptureError(current["detail"])
    if camera.width <= 0 or camera.height <= 0:
        raise CameraCaptureError("camera width and height must be positive")

    # MJPEG avoids a costly raw-YUYV conversion on the Pi.  One frame per
    # dashboard refresh is intentionally modest; this is situational viewing,
    # not a surveillance/video-recording system.
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "v4l2", "-input_format", "mjpeg",
                "-video_size", f"{camera.width}x{camera.height}",
                "-framerate", "5", "-i", camera.device,
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            capture_output=True,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CameraCaptureError(f"camera capture failed: {exc}") from exc
    if result.returncode != 0 or not result.stdout.startswith(b"\xff\xd8"):
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CameraCaptureError(detail or "camera did not return a JPEG")
    return result.stdout


def ensure_storage_dir(camera: CameraConfig) -> Path:
    """Return storage_dir, creating snapshots/events subdirectories.

    storage_dir is just a configured real path on the host filesystem (this
    project runs directly under systemd, not in a container, so there is no
    "ephemeral filesystem" to accidentally land on the way there would be in
    Docker) -- persistence comes from pointing it at real, durable storage in
    config.yaml (e.g. the mounted backup HDD), which is an operator choice
    documented in docs/camera.md#storage, not something this function can
    verify on its own. What this function *does* guard against is a
    filesystem-level failure (permission denied, disk full, path exists as a
    file) -- those surface as CameraStorageUnavailableError so callers can
    report a clear status instead of crashing the capture loop.
    """
    root = Path(camera.storage_dir)
    try:
        (root / SNAPSHOTS_SUBDIR).mkdir(parents=True, exist_ok=True)
        (root / EVENTS_SUBDIR).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CameraStorageUnavailableError(
            f"could not create camera storage at {root}: {exc}. "
            "See docs/camera.md#storage."
        ) from exc
    return root


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def free_bytes(camera: CameraConfig) -> int | None:
    try:
        return shutil.disk_usage(camera.storage_dir if Path(camera.storage_dir).exists() else "/").free
    except OSError:
        return None


def save_snapshot(camera: CameraConfig, image: bytes, *, when: float | None = None) -> Path:
    """Persist one already-captured JPEG under storage_dir/snapshots/<date>/,
    organized by day and named with a microsecond timestamp so concurrent
    saves can never collide."""
    ts = when if when is not None else time.time()
    root = ensure_storage_dir(camera)
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    stamp = time.strftime("%H-%M-%S", time.localtime(ts))
    micros = int((ts - int(ts)) * 1_000_000)
    dest = root / SNAPSHOTS_SUBDIR / day / f"{stamp}-{micros:06d}.jpg"
    _atomic_write_bytes(dest, image)
    return dest


def capture_and_save(camera: CameraConfig, *, when: float | None = None) -> Path:
    """Capture one frame and persist it. Raises CameraCaptureError (device
    problem) or CameraStorageUnavailableError (storage problem); callers
    decide how to log/report each distinctly."""
    image = capture_snapshot(camera)
    return save_snapshot(camera, image, when=when)


@dataclass
class StorageUsage:
    storage_dir: str
    exists: bool
    snapshot_count: int
    snapshot_bytes: int
    event_count: int
    event_bytes: int
    total_bytes: int
    filesystem_free_bytes: int | None
    filesystem_total_bytes: int | None
    low_space_warning: bool

    def as_dict(self) -> dict:
        return {
            "storage_dir": self.storage_dir, "exists": self.exists,
            "snapshot_count": self.snapshot_count, "snapshot_bytes": self.snapshot_bytes,
            "event_count": self.event_count, "event_bytes": self.event_bytes,
            "total_bytes": self.total_bytes,
            "filesystem_free_bytes": self.filesystem_free_bytes,
            "filesystem_total_bytes": self.filesystem_total_bytes,
            "low_space_warning": self.low_space_warning,
        }


def _dir_stats(path: Path) -> tuple[int, int]:
    count = 0
    total = 0
    if not path.exists():
        return 0, 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            count += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return count, total


def storage_usage(camera: CameraConfig) -> StorageUsage:
    root = Path(camera.storage_dir)
    snap_count, snap_bytes = _dir_stats(root / SNAPSHOTS_SUBDIR)
    evt_count_files, evt_bytes = _dir_stats(root / EVENTS_SUBDIR)
    event_count = len(list((root / EVENTS_SUBDIR).iterdir())) if (root / EVENTS_SUBDIR).exists() else 0
    fs_free = fs_total = None
    try:
        usage = shutil.disk_usage(root if root.exists() else "/")
        fs_free, fs_total = usage.free, usage.total
    except OSError:
        pass
    return StorageUsage(
        storage_dir=str(root), exists=root.exists(),
        snapshot_count=snap_count, snapshot_bytes=snap_bytes,
        event_count=event_count, event_bytes=evt_bytes,
        total_bytes=snap_bytes + evt_bytes,
        filesystem_free_bytes=fs_free, filesystem_total_bytes=fs_total,
        low_space_warning=fs_free is not None and fs_free < camera.min_free_bytes,
    )


def _snapshot_capture_time(path: Path) -> float:
    """Recover the capture time this snapshot was actually saved with, from
    its own day-folder + filename (see save_snapshot's layout) rather than
    filesystem mtime -- mtime is not reliable here: a Syncthing/rsync
    transfer, a restore, or simply copying the storage tree can all reset it
    without changing when the frame was really captured. Falls back to
    mtime only for a file that doesn't match the expected naming scheme."""
    try:
        day = path.parent.name  # "YYYY-MM-DD"
        stamp = path.stem.split("-")[:3]  # "HH-MM-SS" (ignore the trailing microseconds field)
        parsed = time.strptime(f"{day} {'-'.join(stamp)}", "%Y-%m-%d %H-%M-%S")
        return time.mktime(parsed)
    except (ValueError, IndexError, OSError):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def cleanup_snapshots(camera: CameraConfig) -> dict:
    """Delete snapshots older than snapshot_retention_days. If free space is
    still below min_free_bytes afterward, keep deleting the oldest remaining
    snapshots (never events/recordings -- see cleanup_events) until either
    space recovers or no snapshots remain. Routine snapshots are considered
    low-value/replaceable by design, unlike motion recordings."""
    root = Path(camera.storage_dir) / SNAPSHOTS_SUBDIR
    deleted = 0
    freed = 0
    if not root.exists():
        return {"deleted": 0, "freed_bytes": 0}
    cutoff = time.time() - camera.snapshot_retention_days * 86400
    files = sorted(
        (p for p in root.rglob("*.jpg") if p.is_file()),
        key=_snapshot_capture_time,
    )
    for p in files:
        captured_at = _snapshot_capture_time(p)
        if captured_at < cutoff:
            try:
                size = p.stat().st_size
                p.unlink()
            except OSError:
                continue
            deleted += 1
            freed += size

    free = free_bytes(camera)
    if free is not None and free < camera.min_free_bytes:
        remaining = sorted(
            (p for p in root.rglob("*.jpg") if p.is_file()),
            key=_snapshot_capture_time,
        )
        for p in remaining:
            if free is not None and free >= camera.min_free_bytes:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                deleted += 1
                freed += size
                free = (free or 0) + size
            except OSError:
                continue

    for day_dir in root.glob("*"):
        if day_dir.is_dir() and not any(day_dir.iterdir()):
            day_dir.rmdir()

    return {"deleted": deleted, "freed_bytes": freed}


def cleanup_events(camera: CameraConfig) -> dict:
    """Delete motion-event directories older than recording_retention_days,
    but only once their meta.json reports synced=True (or sync is disabled
    entirely, in which case nothing protects them). This is the fail-safe
    rule from docs/camera.md: an offline Mac must never cause data loss."""
    import json

    root = Path(camera.storage_dir) / EVENTS_SUBDIR
    deleted = 0
    freed = 0
    protected = 0
    if not root.exists():
        return {"deleted": 0, "freed_bytes": 0, "protected_unsynced": 0}
    cutoff = time.time() - camera.recording_retention_days * 86400
    for event_dir in root.iterdir():
        if not event_dir.is_dir():
            continue
        meta_path = event_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        end_ts = meta.get("end_ts") or meta.get("start_ts") or 0
        if end_ts >= cutoff:
            continue
        if camera.sync_enabled and not meta.get("synced"):
            protected += 1
            continue
        _, size = _dir_stats(event_dir)
        for p in sorted(event_dir.rglob("*"), reverse=True):
            try:
                if p.is_file():
                    p.unlink()
                else:
                    p.rmdir()
            except OSError:
                continue
        try:
            event_dir.rmdir()
        except OSError:
            pass
        deleted += 1
        freed += size
    return {"deleted": deleted, "freed_bytes": freed, "protected_unsynced": protected}


def resolve_snapshot_path(camera: CameraConfig, relative: str) -> Path:
    """Safely resolve a client-supplied snapshot path (e.g. "2026-09-10/12-00-00-000000.jpg")
    within storage_dir/snapshots -- never accepts an absolute or traversal path."""
    root = Path(camera.storage_dir) / SNAPSHOTS_SUBDIR
    try:
        return join_within_root(str(root), relative)
    except PathNotAllowedError:
        raise


def resolve_event_path(camera: CameraConfig, event_id: str, relative: str = "") -> Path:
    root = Path(camera.storage_dir) / EVENTS_SUBDIR
    rel = f"{event_id}/{relative}" if relative else event_id
    try:
        return join_within_root(str(root), rel)
    except PathNotAllowedError:
        raise
