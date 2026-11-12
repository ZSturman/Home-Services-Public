import io
import time

import pytest

from workspace_util import motion
from workspace_util.config import MotionConfig


def _jpeg(gray_value: int, size=(160, 120)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", size, color=gray_value).convert("RGB").save(buf, format="JPEG")
    return buf.getvalue()


def test_changed_fraction_zero_for_identical_frames():
    import numpy as np
    a = np.zeros((10, 10), dtype=np.int16)
    assert motion.changed_fraction(a, a, sensitivity=10) == 0.0


def test_changed_fraction_detects_full_frame_change():
    import numpy as np
    a = np.zeros((10, 10), dtype=np.int16)
    b = np.full((10, 10), 255, dtype=np.int16)
    assert motion.changed_fraction(a, b, sensitivity=10) == 1.0


def test_detector_ignores_tiny_lighting_shift_below_min_area():
    cfg = MotionConfig(sensitivity=5, min_changed_area_fraction=0.5, consecutive_frames=1,
                        analysis_width=20, analysis_height=20)
    detector = motion.MotionDetector(cfg)
    assert detector.observe(_jpeg(100, size=(20, 20))) is False
    # a single small corner brightens slightly -- well under 50% of the frame
    assert detector.observe(_jpeg(102, size=(20, 20))) is False


def test_detector_requires_consecutive_frames_before_triggering():
    cfg = MotionConfig(sensitivity=5, min_changed_area_fraction=0.1, consecutive_frames=3,
                        analysis_width=20, analysis_height=20)
    detector = motion.MotionDetector(cfg)
    detector.observe(_jpeg(50, size=(20, 20)))  # seed baseline
    assert detector.observe(_jpeg(200, size=(20, 20))) is False  # 1st over-threshold frame
    assert detector.observe(_jpeg(200, size=(20, 20))) is False  # 2nd
    assert detector.observe(_jpeg(50, size=(20, 20))) is False   # drops back -- resets the streak
    assert detector.observe(_jpeg(200, size=(20, 20))) is False  # 1st again
    assert detector.observe(_jpeg(200, size=(20, 20))) is False  # 2nd
    assert detector.observe(_jpeg(200, size=(20, 20))) is False  # no change vs previous frame -> fraction 0


def test_detector_triggers_after_consecutive_frames():
    cfg = MotionConfig(sensitivity=5, min_changed_area_fraction=0.1, consecutive_frames=2,
                        analysis_width=20, analysis_height=20)
    detector = motion.MotionDetector(cfg)
    frames = [50, 200, 50, 200]
    triggered = [detector.observe(_jpeg(v, size=(20, 20))) for v in frames]
    # frame0 seeds baseline (False); frame1 changes vs frame0 (1st over-threshold, False);
    # frame2 changes vs frame1 (2nd in a row) -> True; frame3 keeps changing -> still True
    assert triggered == [False, False, True, True]


def test_reset_clears_state():
    cfg = MotionConfig(consecutive_frames=1, min_changed_area_fraction=0.1, analysis_width=20, analysis_height=20)
    detector = motion.MotionDetector(cfg)
    detector.observe(_jpeg(50, size=(20, 20)))
    detector._consecutive = 5
    detector.reset()
    assert detector._prev_gray is None
    assert detector._consecutive == 0


def test_event_lifecycle_create_update_list(tmp_path):
    events_root = tmp_path / "events"
    event_dir = motion.create_event(
        events_root, event_id="evt-1", start_ts=1000.0, camera_device_name="Test Cam",
    )
    meta = motion.read_event_meta(event_dir)
    assert meta["event_id"] == "evt-1"
    assert meta["synced"] is False
    assert meta["end_ts"] is None

    motion.update_event_meta(event_dir, last_motion_ts=1005.0, recording_started=True,
                              recording_path="recording.mp4")
    meta = motion.read_event_meta(event_dir)
    assert meta["recording_started"] is True
    assert meta["last_motion_ts"] == 1005.0

    motion.update_event_meta(event_dir, end_ts=1010.0)
    events = motion.list_events(events_root)
    assert len(events) == 1
    assert events[0]["event_id"] == "evt-1"


def test_list_events_most_recent_first(tmp_path):
    events_root = tmp_path / "events"
    motion.create_event(events_root, event_id="a", start_ts=100.0, camera_device_name="Cam")
    motion.create_event(events_root, event_id="b", start_ts=200.0, camera_device_name="Cam")
    events = motion.list_events(events_root)
    assert [e["event_id"] for e in events] == ["b", "a"]


def test_new_event_id_is_unique():
    ids = {motion.new_event_id() for _ in range(20)}
    assert len(ids) == 20
