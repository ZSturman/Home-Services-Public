import time

from workspace_util.camera_stream import (
    PLAYLIST_NAME,
    build_ffmpeg_command,
    prepare_hls_directory,
    stream_status,
)
from workspace_util.config import CameraConfig


def live_config(tmp_path, **overrides):
    values = {
        "enabled": True,
        "live_stream_enabled": True,
        "live_stream_dir": str(tmp_path / "hls"),
        "live_stream_width": 640,
        "live_stream_height": 480,
        "live_stream_fps": 10,
        "live_stream_bitrate_kbps": 1200,
    }
    values.update(overrides)
    return CameraConfig(**values)


def test_hls_command_uses_native_mjpeg_input_and_safari_h264_output(tmp_path):
    cfg = live_config(tmp_path)
    command = build_ffmpeg_command(cfg)
    assert command[:5] == ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    assert command[command.index("-input_format") + 1] == "mjpeg"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-profile:v") + 1] == "baseline"
    assert command[-1].endswith(PLAYLIST_NAME)


def test_hls_status_requires_fresh_playlist_and_segment(tmp_path):
    cfg = live_config(tmp_path)
    root = prepare_hls_directory(cfg)
    assert stream_status(cfg)["available"] is False
    (root / PLAYLIST_NAME).write_text("#EXTM3U\n")
    segment = root / "segment000001.ts"
    segment.write_bytes(b"segment")
    assert stream_status(cfg)["available"] is True
    old = time.time() - 60
    segment.touch()
    import os
    os.utime(segment, (old, old))
    assert stream_status(cfg)["available"] is False


def test_hls_prepare_removes_only_generated_artifacts(tmp_path):
    cfg = live_config(tmp_path)
    root = prepare_hls_directory(cfg)
    (root / PLAYLIST_NAME).write_text("old")
    (root / "segment000001.ts").write_bytes(b"old")
    keep = root / "keep.txt"
    keep.write_text("keep")
    prepare_hls_directory(cfg)
    assert not (root / PLAYLIST_NAME).exists()
    assert not (root / "segment000001.ts").exists()
    assert keep.read_text() == "keep"
