"""Retention over disposable files and a fixed clock; no camera/FFmpeg needed."""
import pytest

from home_services.cameras.events import EventGrouper
from home_services.cameras.recorder import CameraRecorder, MotionDetector
from home_services.cameras.store import CameraStore
from home_services.config import CameraConfig, CameraDevice, load_config


def test_retention_defaults_and_explicit_overrides(env):
    assert CameraConfig().retention_days == env.config.cameras.retention_days == 30
    assert CameraConfig().continuous_retention_hours == env.config.cameras.continuous_retention_hours == 168
    path = env.root / "override.toml"
    path.write_text('[cameras]\nretention_days = 45\ncontinuous_retention_hours = 240\n')
    config = load_config(path)
    assert config.cameras.retention_days == 45
    assert config.cameras.continuous_retention_hours == 240


@pytest.mark.parametrize("motion,protected,active,days", [
    (False, False, False, 7),
    (True, False, False, 30),
    (True, True, False, 30),
    (True, False, True, 30),
])
def test_retention_cutoffs_preserve_protected_and_active_events(env, motion, protected, active, days):
    s = env.s
    cam = "retention-test"
    end = env.clock.now() - days * 86400
    start = end - 60
    root = s.guard.root_path("recordings", write=True)
    path = root / "retention-test.ts"
    path.write_bytes(b"disposable recording bytes")
    s.db.execute("INSERT INTO camera_chunks (camera_id, start_ts, duration, path, bytes) VALUES (?,?,?,?,?)",
                 (cam, start, 60, path.name, path.stat().st_size))
    if motion:
        s.db.execute("INSERT INTO camera_events (id, camera_id, started_at, last_motion_at, ended_at, protected)"
                     " VALUES (?,?,?,?,?,?)", ("evt_retention", cam, start, end, None if active else end, int(protected)))
    store = CameraStore(s.config, s.db, s.guard, s.clock, s.ids)
    recorder = CameraRecorder(s, CameraDevice(cam, cam), store, EventGrouper(), MotionDetector(25, 0.005, 0.6, 2))

    # Keep a complete chunk through the cutoff; expire it on the next sweep.
    assert recorder.retention()["expired_chunks"] == 0
    assert path.exists()
    env.clock.advance(1)
    expired = not (protected or active)
    assert recorder.retention()["expired_chunks"] == int(expired)
    assert path.exists() is not expired
    assert len(s.db.all("SELECT * FROM camera_chunks WHERE camera_id=?", (cam,))) == int(not expired)
    if motion:
        event = s.db.one("SELECT deleted_at, delete_reason FROM camera_events WHERE id='evt_retention'")
        assert (event["deleted_at"] is not None) is expired
        if expired:
            assert event["delete_reason"] == "expired"
