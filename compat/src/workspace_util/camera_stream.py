"""Supervised, local HLS production for the USB webcam.

This module intentionally creates no HTTP, RTSP, WebRTC, or HLS listener.
FFmpeg owns the camera and writes a short rolling HLS window under ``/run``;
the authenticated dashboard is the only process that serves those files to
the tailnet.  Systemd restarts the process after ordinary camera failures.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from .config import CameraConfig, load_config

PLAYLIST_NAME = "index.m3u8"


def hls_directory(camera: CameraConfig) -> Path:
    return Path(camera.live_stream_dir)


def prepare_hls_directory(camera: CameraConfig) -> Path:
    """Remove only stale generated HLS artifacts from the configured runtime dir."""
    root = hls_directory(camera)
    root.mkdir(parents=True, exist_ok=True)
    for entry in root.iterdir():
        if entry.is_file() and (entry.name == PLAYLIST_NAME or entry.suffix in {".ts", ".tmp"}):
            entry.unlink(missing_ok=True)
    return root


def build_ffmpeg_command(camera: CameraConfig) -> list[str]:
    """Build a Safari-compatible, low-latency H.264/HLS capture command."""
    fps = max(1, int(camera.live_stream_fps))
    segment_seconds = max(1, int(camera.live_stream_hls_time_seconds))
    list_size = max(3, int(camera.live_stream_hls_list_size))
    bitrate = max(200, int(camera.live_stream_bitrate_kbps))
    root = hls_directory(camera)
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "v4l2",
        "-thread_queue_size", "512",
        "-input_format", "mjpeg",
        "-video_size", f"{camera.live_stream_width}x{camera.live_stream_height}",
        "-framerate", str(fps),
        "-i", camera.device,
        "-an",
        # Pi 5 has no usable H.264 V4L2 encoder.  At these conservative camera
        # settings libx264 ultrafast is reliable and keeps Safari compatibility.
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-profile:v", "baseline",
        "-pix_fmt", "yuv420p",
        "-b:v", f"{bitrate}k",
        "-maxrate", f"{int(bitrate * 1.2)}k",
        "-bufsize", f"{bitrate * 2}k",
        "-g", str(fps * segment_seconds),
        "-keyint_min", str(fps * segment_seconds),
        "-sc_threshold", "0",
        "-f", "hls",
        "-hls_time", str(segment_seconds),
        "-hls_list_size", str(list_size),
        "-hls_flags", "delete_segments+append_list+independent_segments+temp_file",
        "-hls_segment_filename", str(root / "segment%06d.ts"),
        str(root / PLAYLIST_NAME),
    ]


def stream_status(camera: CameraConfig) -> dict:
    """Report fresh output rather than merely a running service process."""
    if not camera.enabled or not camera.live_stream_enabled:
        return {"enabled": False, "available": False, "detail": "Live camera streaming is disabled."}
    playlist = hls_directory(camera) / PLAYLIST_NAME
    try:
        segment_times = [p.stat().st_mtime for p in hls_directory(camera).glob("*.ts")]
        newest = max(segment_times)
    except (OSError, ValueError):
        return {"enabled": True, "available": False, "detail": "Camera is starting or reconnecting."}
    max_age = max(12, camera.live_stream_hls_time_seconds * camera.live_stream_hls_list_size + 8)
    age = max(0.0, time.time() - newest)
    available = playlist.is_file() and age <= max_age
    return {
        "enabled": True,
        "available": available,
        "detail": "Live stream ready" if available else "Camera is reconnecting.",
        "width": camera.live_stream_width,
        "height": camera.live_stream_height,
        "fps": camera.live_stream_fps,
        "playlist": PLAYLIST_NAME if available else None,
        "latest_segment_age_seconds": round(age, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the private camera HLS producer")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    cfg = load_config(args.config)
    camera = cfg.camera
    if not camera.enabled or not camera.live_stream_enabled:
        print("live camera stream disabled by configuration", file=sys.stderr)
        return
    if not Path(camera.device).exists():
        print(f"camera device unavailable: {camera.device}", file=sys.stderr)
        raise SystemExit(2)
    if not shutil.which("ffmpeg"):
        print("ffmpeg is required for the live camera stream", file=sys.stderr)
        raise SystemExit(2)
    try:
        prepare_hls_directory(camera)
    except OSError as exc:
        print(f"cannot prepare HLS runtime directory: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    command = build_ffmpeg_command(camera)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
