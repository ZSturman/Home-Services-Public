import subprocess
import time

import pytest

from workspace_util import camera
from workspace_util.config import CameraConfig


def test_disabled_camera_is_not_available():
    status = camera.status(CameraConfig(enabled=False))

    assert status["available"] is False
    assert status["detail"] == "camera disabled in config"


def test_snapshot_uses_one_mjpeg_frame(monkeypatch, tmp_path):
    device = tmp_path / "video0"
    device.touch()
    cfg = CameraConfig(enabled=True, device=str(device), width=640, height=480)
    monkeypatch.setattr(camera.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    captured = {}

    class Result:
        returncode = 0
        stdout = b"\xff\xd8jpeg-data"
        stderr = b""

    def fake_run(args, capture_output, timeout):
        captured["args"] = args
        return Result()

    monkeypatch.setattr(camera.subprocess, "run", fake_run)

    assert camera.capture_snapshot(cfg) == b"\xff\xd8jpeg-data"
    assert "-input_format" in captured["args"]
    assert "mjpeg" in captured["args"]
    assert "-frames:v" in captured["args"]


def test_snapshot_rejects_invalid_capture(monkeypatch, tmp_path):
    device = tmp_path / "video0"
    device.touch()
    cfg = CameraConfig(enabled=True, device=str(device))
    monkeypatch.setattr(camera.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        camera.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"bad camera"),
    )

    with pytest.raises(camera.CameraCaptureError, match="bad camera"):
        camera.capture_snapshot(cfg)


def test_save_snapshot_organizes_by_date_with_unique_timestamped_names(tmp_path):
    cfg = CameraConfig(storage_dir=str(tmp_path / "camstore"))
    when = time.mktime(time.strptime("2026-09-10 12:30:00", "%Y-%m-%d %H:%M:%S"))
    p1 = camera.save_snapshot(cfg, b"\xff\xd8one", when=when)
    p2 = camera.save_snapshot(cfg, b"\xff\xd8two", when=when + 0.0001)
    assert p1.parent.name == "2026-09-10"
    assert p1 != p2  # no collision even for near-identical timestamps
    assert p1.read_bytes() == b"\xff\xd8one"
    assert p2.read_bytes() == b"\xff\xd8two"


def test_ensure_storage_dir_creates_fresh_directory(tmp_path):
    cfg = CameraConfig(storage_dir=str(tmp_path / "not-yet-created" / "camera-storage"))
    root = camera.ensure_storage_dir(cfg)
    assert root.is_dir()
    assert (root / camera.SNAPSHOTS_SUBDIR).is_dir()


def test_ensure_storage_dir_wraps_filesystem_errors(tmp_path, monkeypatch):
    cfg = CameraConfig(storage_dir=str(tmp_path / "camstore"))

    def fail_mkdir(self, *a, **k):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(camera.Path, "mkdir", fail_mkdir)
    with pytest.raises(camera.CameraStorageUnavailableError):
        camera.ensure_storage_dir(cfg)


def test_ensure_storage_dir_allows_already_existing_directory(tmp_path):
    storage = tmp_path / "camstore"
    storage.mkdir()
    cfg = CameraConfig(storage_dir=str(storage))
    root = camera.ensure_storage_dir(cfg)
    assert (root / camera.SNAPSHOTS_SUBDIR).is_dir()
    assert (root / camera.EVENTS_SUBDIR).is_dir()


def test_capture_and_save_persists_captured_frame(monkeypatch, tmp_path):
    device = tmp_path / "video0"
    device.touch()
    cfg = CameraConfig(enabled=True, device=str(device), storage_dir=str(tmp_path / "camstore"))
    monkeypatch.setattr(camera.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    class Result:
        returncode = 0
        stdout = b"\xff\xd8jpeg-data"
        stderr = b""

    monkeypatch.setattr(camera.subprocess, "run", lambda *a, **k: Result())
    path = camera.capture_and_save(cfg)
    assert path.read_bytes() == b"\xff\xd8jpeg-data"


def test_cleanup_snapshots_deletes_only_past_retention(tmp_path):
    cfg = CameraConfig(storage_dir=str(tmp_path / "camstore"), snapshot_retention_days=1)
    now = time.time()
    old = camera.save_snapshot(cfg, b"\xff\xd8old", when=now - 5 * 86400)
    recent = camera.save_snapshot(cfg, b"\xff\xd8new", when=now)
    result = camera.cleanup_snapshots(cfg)
    assert result["deleted"] == 1
    assert not old.exists()
    assert recent.exists()


def test_cleanup_snapshots_frees_space_when_below_min_free_even_if_recent(tmp_path, monkeypatch):
    cfg = CameraConfig(storage_dir=str(tmp_path / "camstore"), snapshot_retention_days=365,
                        min_free_bytes=10**15)  # an unreachable floor forces the low-space branch
    now = time.time()
    a = camera.save_snapshot(cfg, b"\xff\xd8" + b"a" * 100, when=now - 10)
    b = camera.save_snapshot(cfg, b"\xff\xd8" + b"b" * 100, when=now)
    result = camera.cleanup_snapshots(cfg)
    assert result["deleted"] == 2  # both are recent, but low free space forces cleanup
    assert not a.exists() and not b.exists()


def test_cleanup_events_protects_unsynced_recordings_when_sync_enabled(tmp_path):
    from workspace_util import motion

    cfg = CameraConfig(storage_dir=str(tmp_path / "camstore"), recording_retention_days=1, sync_enabled=True)
    events_root = tmp_path / "camstore" / camera.EVENTS_SUBDIR
    now = time.time()
    old_unsynced = motion.create_event(events_root, event_id="old-unsynced", start_ts=now - 10 * 86400,
                                        camera_device_name="Cam")
    motion.update_event_meta(old_unsynced, end_ts=now - 10 * 86400, synced=False)
    old_synced = motion.create_event(events_root, event_id="old-synced", start_ts=now - 10 * 86400,
                                      camera_device_name="Cam")
    motion.update_event_meta(old_synced, end_ts=now - 10 * 86400, synced=True)

    result = camera.cleanup_events(cfg)
    assert result["deleted"] == 1
    assert result["protected_unsynced"] == 1
    assert old_unsynced.exists()
    assert not old_synced.exists()


def test_cleanup_events_deletes_old_unsynced_when_sync_disabled(tmp_path):
    from workspace_util import motion

    cfg = CameraConfig(storage_dir=str(tmp_path / "camstore"), recording_retention_days=1, sync_enabled=False)
    events_root = tmp_path / "camstore" / camera.EVENTS_SUBDIR
    now = time.time()
    old = motion.create_event(events_root, event_id="old", start_ts=now - 10 * 86400, camera_device_name="Cam")
    motion.update_event_meta(old, end_ts=now - 10 * 86400, synced=False)

    result = camera.cleanup_events(cfg)
    assert result["deleted"] == 1
    assert not old.exists()


def test_storage_usage_reports_counts_and_low_space_warning(tmp_path):
    cfg = CameraConfig(storage_dir=str(tmp_path / "camstore"), min_free_bytes=10**15)
    camera.save_snapshot(cfg, b"\xff\xd8" + b"x" * 50)
    usage = camera.storage_usage(cfg)
    assert usage.snapshot_count == 1
    assert usage.total_bytes >= 50
    assert usage.low_space_warning is True


def test_resolve_snapshot_path_rejects_traversal(tmp_path):
    cfg = CameraConfig(storage_dir=str(tmp_path / "camstore"))
    camera.ensure_storage_dir(cfg)
    with pytest.raises(Exception):
        camera.resolve_snapshot_path(cfg, "../../../etc/passwd")


def test_resolve_event_path_rejects_traversal(tmp_path):
    cfg = CameraConfig(storage_dir=str(tmp_path / "camstore"))
    camera.ensure_storage_dir(cfg)
    with pytest.raises(Exception):
        camera.resolve_event_path(cfg, "evt-1", "../../../etc/passwd")
