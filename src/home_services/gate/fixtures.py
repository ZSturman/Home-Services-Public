"""Local RTSP fixture, candidate engine config, and resource sampling."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .synthetic import probe


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_mediamtx() -> str | None:
    """mediamtx binary: $HOME_SERVICES_GATE_MEDIAMTX, then PATH, then the
    checkout's ignored .dev/tools/ (see docs/CAMERA_GATE.md)."""
    explicit = os.environ.get("HOME_SERVICES_GATE_MEDIAMTX")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    found = shutil.which("mediamtx")
    if found:
        return found
    local = Path(__file__).resolve().parents[3] / ".dev" / "tools" / "mediamtx"
    return str(local) if local.is_file() else None


class RtspFixture:
    """Serve a looping clip on loopback RTSP: mediamtx listens on 127.0.0.1
    (TCP only, every other protocol disabled) and FFmpeg publishes the file
    into it. Only ever loops a file, never a device."""

    def __init__(self, clip: Path, work_dir: Path, port: int | None = None, path: str = "fixture"):
        if str(clip).startswith("/dev/"):
            raise ValueError("the gate never opens a device")
        self.clip, self.work_dir = clip, work_dir
        self.port = port or free_port()
        self.url = f"rtsp://127.0.0.1:{self.port}/{path}"
        self.procs: list[subprocess.Popen] = []

    def __enter__(self):
        binary = find_mediamtx()
        if binary is None:
            raise RuntimeError("mediamtx not found; set HOME_SERVICES_GATE_MEDIAMTX or install it (docs/CAMERA_GATE.md)")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        cfg = self.work_dir / "mediamtx.yml"
        cfg.write_text("\n".join([
            "logLevel: warn", f"rtspAddress: 127.0.0.1:{self.port}", "rtspTransports: [tcp]",
            "rtmp: no", "hls: no", "webrtc: no", "srt: no", "moq: no", "api: no", "metrics: no", "pprof: no", "playback: no",
            "paths:", "  all_others:", ""]))
        self.procs.append(subprocess.Popen([binary, str(cfg)], cwd=self.work_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        self._wait_port()
        self.procs.append(subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", str(self.clip),
             "-c", "copy", "-f", "rtsp", "-rtsp_transport", "tcp", self.url],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        time.sleep(1.5)
        for p in self.procs:
            if p.poll() is not None:
                err = p.stderr.read().decode(errors="replace")[-500:] if p.stderr else ""
                self.__exit__()
                raise RuntimeError(f"RTSP fixture process exited: {err}")
        return self

    def _wait_port(self, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.1)
        raise RuntimeError("mediamtx did not start listening")

    def __exit__(self, *exc):
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.procs.clear()
        return False

    def probe(self) -> dict:
        return probe(self.url)

    def probe_concurrent(self, consumers: int = 2) -> list[dict]:
        """Open several consumers at once against the restream."""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(consumers) as ex:
            return list(ex.map(lambda _: probe(self.url), range(consumers)))


def candidate_frigate_config(rtsp_urls: dict[str, str], record_dir_note: str = "") -> str:
    """Render the candidate engine configuration the gate evaluates.

    Intent: go2rtc restreams each source; Frigate decodes a low-resolution
    detect stream for motion only (no object labels tracked, so no object
    inference is needed), records on motion, and keeps admin/media listeners
    private. Field names must be confirmed against the pinned Frigate version
    by the `engine_config_accepted` step; this text is a candidate, not a
    verified configuration."""
    lines = ["mqtt:", "  enabled: false", "go2rtc:", "  streams:"]
    for name, url in rtsp_urls.items():
        lines += [f"    {name}:", f"      - {url}"]
    lines += ["detectors:", "  cpu1:", "    type: cpu", "objects:", "  track: []",
              "record:", "  enabled: true", "  retain:", "    days: 1", "    mode: motion", "cameras:"]
    for name in rtsp_urls:
        lines += [f"  {name}:", "    ffmpeg:", "      inputs:",
                  f"        - path: rtsp://127.0.0.1:8554/{name}", "          input_args: preset-rtsp-restream",
                  "          roles: [detect, record]",
                  "    detect:", "      enabled: true", "      width: 320", "      height: 180", "      fps: 5",
                  "    motion:", "      threshold: 30", "      contour_area: 10"]
    if record_dir_note:
        lines.append(f"# {record_dir_note}")
    return "\n".join(lines) + "\n"


def read_resources(meminfo: str = "/proc/meminfo", loadavg: str = "/proc/loadavg") -> dict | None:
    try:
        mem = Path(meminfo).read_text()
        load = Path(loadavg).read_text().split()
    except OSError:
        return None
    avail_kb = next(int(line.split()[1]) for line in mem.splitlines() if line.startswith("MemAvailable:"))
    return {"mem_available_bytes": avail_kb * 1024, "load1": float(load[0]), "load5": float(load[1])}


def evaluate_samples(samples: list[dict], *, min_available_bytes: int = 500_000_000, max_load5: float = 4.0) -> dict:
    if not samples:
        return {"passed": False, "reason": "no samples"}
    min_avail = min(s["mem_available_bytes"] for s in samples)
    max_l5 = max(s["load5"] for s in samples)
    return {"passed": min_avail >= min_available_bytes and max_l5 < max_load5, "samples": len(samples),
            "min_mem_available_bytes": min_avail, "max_load5": max_l5,
            "thresholds": {"min_mem_available_bytes": min_available_bytes, "max_load5_exclusive": max_load5}}


def sample_for(seconds: float, interval: float = 10.0, reader=read_resources, sleep=time.sleep) -> list[dict]:
    samples = []
    end = time.monotonic() + seconds
    while True:
        r = reader()
        if r is None:
            return []
        samples.append({**r, "t": time.time()})
        if time.monotonic() >= end:
            return samples
        sleep(interval)


def is_linux() -> bool:
    return os.uname().sysname == "Linux"
