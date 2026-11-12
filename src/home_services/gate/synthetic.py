"""Synthetic gate media and ground-truth motion analysis.

Two H.264 clips are generated with FFmpeg's lavfi sources, never a camera:
  no_motion.mp4 - flat scene with light temporal sensor noise
  motion.mp4    - the same scene with a bright square crossing it only
                  during [motion_start, motion_end)
The analyzer decodes downscaled grayscale frames and applies simple frame
differencing, so the fixtures' motion windows are verified independently of
any engine before they are used to judge one.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ANALYSIS_W, ANALYSIS_H = 160, 90


@dataclass(frozen=True)
class ClipSpec:
    width: int = 1280
    height: int = 720
    fps: int = 10
    seconds: int = 30
    motion_start: int = 10
    motion_end: int = 20


def require_tools() -> dict:
    found = {t: shutil.which(t) for t in ("ffmpeg", "ffprobe")}
    return found


def _encode_args(spec: ClipSpec, out: Path) -> list[str]:
    return ["-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", str(spec.fps * 2),
            "-r", str(spec.fps), "-t", str(spec.seconds), "-an", "-y", str(out)]


def generate(out_dir: Path, spec: ClipSpec = ClipSpec()) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    bg = f"color=c=0x404040:s={spec.width}x{spec.height}:r={spec.fps}:d={spec.seconds},noise=alls=3:allf=t"
    no_motion = out_dir / "no_motion.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", bg, *_encode_args(spec, no_motion)], check=True)
    box = spec.height // 4
    # A tenth of the frame width per frame, wrapping: unambiguous motion at any
    # resolution (slow sub-threshold motion is an engine-tuning question, not
    # a ground-truth one).
    speed = spec.width * spec.fps / 10
    graph = (f"{bg}[bg];color=c=white:s={box}x{box}:r={spec.fps}:d={spec.seconds}[box];"
             f"[bg][box]overlay=x='mod((t-{spec.motion_start})*{speed:.3f},{spec.width - box})':y={(spec.height - box) // 2}:"
             f"enable='between(t,{spec.motion_start},{spec.motion_end - 0.001})'")
    motion = out_dir / "motion.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", graph, *_encode_args(spec, motion)], check=True)
    return {"no_motion": no_motion, "motion": motion}


def probe(path_or_url: str, timeout: int = 20) -> dict:
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=codec_name,profile,width,height,avg_frame_rate", "-of", "json", path_or_url],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-500:])
    import json
    streams = json.loads(proc.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("no video stream")
    return streams[0]


def frames(path: Path, fps: int) -> list[bytes]:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf",
                           f"fps={fps},scale={ANALYSIS_W}:{ANALYSIS_H},format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, check=True)
    size = ANALYSIS_W * ANALYSIS_H
    data = proc.stdout
    return [data[i:i + size] for i in range(0, len(data) - size + 1, size)]


def motion_seconds(frame_list: list[bytes], fps: int, threshold: int = 25, min_fraction: float = 0.01) -> list[float]:
    """Timestamps (seconds) of frames whose changed-pixel fraction against the
    previous frame exceeds min_fraction."""
    hits = []
    n = ANALYSIS_W * ANALYSIS_H
    for i in range(1, len(frame_list)):
        a, b = frame_list[i - 1], frame_list[i]
        changed = sum(1 for x, y in zip(a, b) if abs(x - y) > threshold)
        if changed / n > min_fraction:
            hits.append(i / fps)
    return hits


def verify_ground_truth(clips: dict[str, Path], spec: ClipSpec = ClipSpec()) -> dict:
    fps = spec.fps
    nm = motion_seconds(frames(clips["no_motion"], fps), fps)
    m = motion_seconds(frames(clips["motion"], fps), fps)
    inside = [t for t in m if spec.motion_start <= t <= spec.motion_end + 1 / fps]
    outside = [t for t in m if t not in inside]
    ok = not nm and not outside and len(inside) >= (spec.motion_end - spec.motion_start) * fps * 0.8
    return {"passed": ok, "no_motion_hits": len(nm), "motion_hits_inside_window": len(inside),
            "motion_hits_outside_window": len(outside), "window": [spec.motion_start, spec.motion_end]}
