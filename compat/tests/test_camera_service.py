import json
import time

import pytest

from workspace_util import camera as camera_mod
from workspace_util import camera_service
from workspace_util import motion as motion_mod
from workspace_util.camera_service import CameraService
from workspace_util.config import (
    BackupConfig, CameraConfig, Config, DashboardConfig, DiskConfig, LoggingConfig,
    MacBridgeConfig, MotionConfig, NotificationsConfig, SyncConfig, WorkspaceConfig,
)
from pathlib import Path


def make_config(tmp_path, **camera_overrides) -> Config:
    defaults = dict(
        enabled=True, device="/fake/video0", storage_dir=str(tmp_path / "camstore"),
        refresh_seconds=100,  # long enough that tests control timing explicitly
        motion=MotionConfig(sample_interval_seconds=100, consecutive_frames=2,
                             min_changed_area_fraction=0.1, sensitivity=10,
                             analysis_width=20, analysis_height=20,
                             recording_seconds_after_motion=5, max_recording_seconds=60),
    )
    defaults.update(camera_overrides)
    camera_cfg = CameraConfig(**defaults)
    return Config(
        workspace=WorkspaceConfig(local_mac_path=str(tmp_path / "mac"), pi_path=str(tmp_path / "pi")),
        disk=DiskConfig(), sync=SyncConfig(), backup=BackupConfig(),
        dashboard=DashboardConfig(), mac_bridge=MacBridgeConfig(),
        notifications=NotificationsConfig(backends=[{"type": "log"}]),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
        commands={}, source_path=tmp_path / "config.yaml",
        camera=camera_cfg,
    )


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, level, subject, body):
        self.sent.append((level, subject, body))


def _jpeg(gray_value: int, size=(20, 20)) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", size, color=gray_value).convert("RGB").save(buf, format="JPEG")
    return buf.getvalue()


def test_out_of_house_persists_across_service_instances(tmp_path):
    cfg = make_config(tmp_path)
    svc1 = CameraService(cfg, notifier=FakeNotifier())
    assert svc1.armed is False
    svc1.arm(True)

    svc2 = CameraService(cfg, notifier=FakeNotifier())
    assert svc2.armed is True  # loaded from camera_state.json, not just in-memory


def test_unarmed_idle_saves_periodic_snapshot(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    svc = CameraService(cfg, notifier=FakeNotifier())
    monkeypatch.setattr(camera_mod, "capture_snapshot", lambda c: _jpeg(100))

    svc._service_unarmed(time.time())

    assert svc.last_snapshot_path is not None
    assert Path(svc.last_snapshot_path).exists()


def test_camera_capture_failure_does_not_raise_and_is_tracked(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    notifier = FakeNotifier()
    svc = CameraService(cfg, notifier=notifier)

    def fail(c):
        raise camera_mod.CameraCaptureError("device not found")

    monkeypatch.setattr(camera_mod, "capture_snapshot", fail)

    for _ in range(3):
        svc._service_unarmed(time.time())
        svc.last_snapshot_at = None  # force another attempt each call

    assert svc.consecutive_failures == 3
    assert svc.last_capture_error == "device not found"
    assert any(subject == "Camera unavailable" for _, subject, _ in notifier.sent)


def test_armed_idle_triggers_motion_event_after_consecutive_frames(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    notifier = FakeNotifier()
    svc = CameraService(cfg, notifier=notifier)
    svc.armed = True

    # Frame-to-frame differencing needs *changing* frames to keep counting
    # consecutively -- alternating values simulates real motion better than
    # a single repeated "different" frame would (see test_motion.py).
    frames = iter([_jpeg(50), _jpeg(220), _jpeg(50), _jpeg(220)])
    monkeypatch.setattr(camera_mod, "capture_snapshot", lambda c: next(frames))
    monkeypatch.setattr(svc, "_start_recording", lambda event_dir, cfg: True)

    now = time.time()
    for i in range(4):
        svc.last_sample_at = None  # allow the next sample immediately
        svc._service_armed_idle(now + i)

    assert svc.current_event is not None
    assert notifier.sent  # notified exactly once for the whole event
    assert len(notifier.sent) == 1

    meta = motion_mod.read_event_meta(svc.current_event["dir"])
    assert meta["recording_started"] is True
    assert len(meta["pre_context"]) >= 1  # pre-event frames captured before the trigger


def test_recording_finalizes_after_quiet_period(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    svc = CameraService(cfg, notifier=FakeNotifier())

    class FakeProc:
        def poll(self):
            return None  # still "running"

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    svc.recording_process = FakeProc()
    root = camera_mod.ensure_storage_dir(cfg.camera)
    event_dir = motion_mod.create_event(
        root / camera_mod.EVENTS_SUBDIR, event_id="evt-quiet", start_ts=time.time() - 100,
        camera_device_name="Test Cam",
    )
    (event_dir / motion_mod.RECORDING_FILENAME).write_bytes(b"fake-mp4-data")
    svc.current_event = {"id": "evt-quiet", "dir": event_dir, "started_at": time.time() - 100}
    svc.last_motion_at = time.time() - 100  # well past recording_seconds_after_motion=5

    svc._service_recording(time.time())

    assert svc.current_event is None
    meta = motion_mod.read_event_meta(event_dir)
    assert meta["end_ts"] is not None
    assert meta["recording_started"] is True


def test_recording_respects_max_duration_cap(tmp_path):
    cfg = make_config(tmp_path)
    svc = CameraService(cfg, notifier=FakeNotifier())

    class FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    svc.recording_process = FakeProc()
    root = camera_mod.ensure_storage_dir(cfg.camera)
    started_at = time.time() - 61  # past max_recording_seconds=60
    event_dir = motion_mod.create_event(
        root / camera_mod.EVENTS_SUBDIR, event_id="evt-cap", start_ts=started_at,
        camera_device_name="Test Cam",
    )
    svc.current_event = {"id": "evt-cap", "dir": event_dir, "started_at": started_at}
    svc.last_motion_at = time.time()  # motion still "active" -- the hard cap should still apply

    svc._service_recording(time.time())

    assert svc.current_event is None  # forced closed despite continued motion


def test_capture_on_demand_rejects_while_recording(tmp_path):
    cfg = make_config(tmp_path)
    svc = CameraService(cfg, notifier=FakeNotifier())
    svc.current_event = {"id": "evt-1", "dir": tmp_path, "started_at": time.time()}

    with pytest.raises(camera_mod.CameraBusyError):
        svc.capture_on_demand()


def test_capture_on_demand_rejects_when_device_lock_held(tmp_path):
    cfg = make_config(tmp_path)
    svc = CameraService(cfg, notifier=FakeNotifier())
    svc._device_lock.acquire()
    try:
        with pytest.raises(camera_mod.CameraBusyError):
            svc.capture_on_demand()
    finally:
        svc._device_lock.release()


def test_start_is_idempotent_does_not_spawn_a_second_thread(tmp_path):
    cfg = make_config(tmp_path, enabled=False)  # tick() no-ops; only lifecycle is under test
    svc = CameraService(cfg, notifier=FakeNotifier())
    svc.start()
    thread_1 = svc._thread
    svc.start()
    thread_2 = svc._thread
    assert thread_1 is thread_2
    assert thread_1.is_alive()
    svc.stop()
    assert not thread_1.is_alive()


def test_stop_finalizes_an_in_progress_event(tmp_path):
    cfg = make_config(tmp_path)
    svc = CameraService(cfg, notifier=FakeNotifier())

    class FakeProc:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    proc = FakeProc()
    svc.recording_process = proc
    root = camera_mod.ensure_storage_dir(cfg.camera)
    event_dir = motion_mod.create_event(
        root / camera_mod.EVENTS_SUBDIR, event_id="evt-restart", start_ts=time.time(),
        camera_device_name="Test Cam",
    )
    svc.current_event = {"id": "evt-restart", "dir": event_dir, "started_at": time.time()}

    svc.stop()

    assert proc.terminated is True
    meta = motion_mod.read_event_meta(event_dir)
    assert meta["end_ts"] is not None  # never left dangling as "active" across a restart


def test_mark_synced_if_caught_up_protects_recent_and_still_pending_events(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, sync_enabled=True)
    svc = CameraService(cfg, notifier=FakeNotifier())
    root = camera_mod.ensure_storage_dir(cfg.camera)
    events_root = root / camera_mod.EVENTS_SUBDIR
    now = time.time()

    long_closed = motion_mod.create_event(events_root, event_id="old", start_ts=now - 200, camera_device_name="Cam")
    motion_mod.update_event_meta(long_closed, end_ts=now - 200)  # closed well past the 60s grace period

    just_closed = motion_mod.create_event(events_root, event_id="recent", start_ts=now - 5, camera_device_name="Cam")
    motion_mod.update_event_meta(just_closed, end_ts=now - 5)  # inside the grace period

    from workspace_util import camera_sync
    monkeypatch.setattr(camera_sync, "folder_sync_status", lambda cfg: {"configured": True, "need_files": 0})

    svc._mark_synced_if_caught_up()

    assert motion_mod.read_event_meta(long_closed)["synced"] is True
    assert motion_mod.read_event_meta(just_closed)["synced"] is False  # too recent -- grace period protects it


def test_mark_synced_if_caught_up_does_nothing_while_backlog_pending(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, sync_enabled=True)
    svc = CameraService(cfg, notifier=FakeNotifier())
    root = camera_mod.ensure_storage_dir(cfg.camera)
    events_root = root / camera_mod.EVENTS_SUBDIR
    now = time.time()
    event_dir = motion_mod.create_event(events_root, event_id="pending", start_ts=now - 200, camera_device_name="Cam")
    motion_mod.update_event_meta(event_dir, end_ts=now - 200)

    from workspace_util import camera_sync
    monkeypatch.setattr(camera_sync, "folder_sync_status", lambda cfg: {"configured": True, "need_files": 4})

    svc._mark_synced_if_caught_up()

    assert motion_mod.read_event_meta(event_dir)["synced"] is False  # Mac still has a backlog -- must stay protected


def test_status_dict_reports_camera_and_motion_fields(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    svc = CameraService(cfg, notifier=FakeNotifier())
    monkeypatch.setattr(camera_mod, "shutil", camera_mod.shutil)
    status = svc.status_dict()
    assert status["out_of_house"] is False
    assert status["recording"] is False
    assert "last_snapshot_at" in status
    assert "last_motion_at" in status
