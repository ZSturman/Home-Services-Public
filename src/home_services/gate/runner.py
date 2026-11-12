"""Feasibility-gate runner.

Each step records `pass`, `fail`, or `not_run` with evidence. The gate passes
only when every required step passes; otherwise the report names the first
failing or unrun step so the smallest next experiment is explicit. The
runner never opens a camera device and never touches existing camera
services: sources are generated files served on loopback.
"""
from __future__ import annotations

import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .fixtures import RtspFixture, candidate_frigate_config, evaluate_samples, is_linux, sample_for
from .synthetic import ClipSpec, generate, probe, require_tools, verify_ground_truth

REQUIRED_STEPS = (
    "tools", "synthetic_media", "ground_truth_motion", "rtsp_fixture", "no_device_access", "candidate_config",
    "engine_reachable", "engine_camera_fps", "engine_motion_signal", "engine_concurrent_live_view",
    "engine_history_playback_export", "engine_interval_deletion", "resource_benchmark",
)
ENGINE_MANUAL = {
    "engine_motion_signal": "Verify per-camera motion ON/OFF follows the motion clip's window and stays OFF for no_motion "
                            "using the engine's motion signal (not object events). Record the transport used.",
    "engine_concurrent_live_view": "Hold two live consumers on the restream while recording runs; record dropped frames.",
    "engine_history_playback_export": "Query recordings for the motion window, play one segment, export the window, and hash the export.",
    "engine_interval_deletion": "Delete one recorded interval via a supported API and confirm the rest remains playable.",
}


def _step(name: str, status: str, **evidence) -> dict:
    return {"step": name, "status": status, **evidence}


def run_gate(out_dir: Path, *, engine_url: str | None = None, engine_cameras: list[str] | None = None,
             benchmark_seconds: float = 0, spec: ClipSpec = ClipSpec(), evidence: dict[str, Path] | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []
    tools = require_tools()
    steps.append(_step("tools", "pass" if all(tools.values()) else "fail", found=tools))
    clips = None
    if all(tools.values()):
        try:
            clips = generate(out_dir / "media", spec)
            steps.append(_step("synthetic_media", "pass", clips={k: str(v) for k, v in clips.items()},
                               probe={k: probe(str(v)) for k, v in clips.items()}))
        except Exception as exc:  # noqa: BLE001
            steps.append(_step("synthetic_media", "fail", error=str(exc)))
    if clips:
        gt = verify_ground_truth(clips, spec)
        steps.append(_step("ground_truth_motion", "pass" if gt["passed"] else "fail", **gt))
        try:
            with RtspFixture(clips["motion"], out_dir / "rtsp") as fx:
                info = fx.probe()
                concurrent = fx.probe_concurrent(2)
            steps.append(_step("rtsp_fixture", "pass", url_scheme="rtsp", host="127.0.0.1", probe=info, concurrent_consumers=len(concurrent),
                               note="loopback mediamtx fixture; engine concurrency is evaluated separately"))
        except Exception as exc:  # noqa: BLE001
            steps.append(_step("rtsp_fixture", "fail", error=str(exc)))
    sources = {"gate_motion": "rtsp://127.0.0.1:8555/fixture", "gate_no_motion": "rtsp://127.0.0.1:8556/fixture"}
    steps.append(_step("no_device_access", "pass" if not any("/dev/" in u for u in sources.values()) else "fail",
                       sources=sources, note="the gate never opens /dev/video*; the live USB producer is untouched"))
    config_text = candidate_frigate_config(sources, "candidate only; confirm keys against the pinned engine version")
    (out_dir / "frigate-candidate.yml").write_text(config_text)
    steps.append(_step("candidate_config", "pass", path=str(out_dir / "frigate-candidate.yml")))

    if engine_url:
        cams = engine_cameras or list(sources)
        try:
            with httpx.Client(base_url=engine_url, timeout=10) as client:
                version = client.get("/api/version")
                version.raise_for_status()
                steps.append(_step("engine_reachable", "pass", version=version.text.strip()))
                stats = client.get("/api/stats").json()
                fps = {c: (stats.get("cameras", {}).get(c, {}) or {}).get("camera_fps") for c in cams}
                ok = all(isinstance(v, (int, float)) and v > 0 for v in fps.values())
                steps.append(_step("engine_camera_fps", "pass" if ok else "fail", camera_fps=fps))
        except Exception as exc:  # noqa: BLE001
            steps.append(_step("engine_reachable", "fail", error=f"{type(exc).__name__}: {exc}"))
    else:
        steps.append(_step("engine_reachable", "not_run", reason="no --engine-url; run on the target host with the pinned engine"))
        steps.append(_step("engine_camera_fps", "not_run", reason="engine not reachable"))
    for name, how in ENGINE_MANUAL.items():
        ev = (evidence or {}).get(name)
        if ev and ev.is_file():
            record = json.loads(ev.read_text())
            steps.append(_step(name, "pass" if record.get("passed") is True else "fail", evidence_file=str(ev), evidence=record))
        else:
            steps.append(_step(name, "not_run", procedure=how, reason="requires the engine on the target host; supply --evidence"))

    if benchmark_seconds and is_linux():
        samples = sample_for(benchmark_seconds)
        result = evaluate_samples(samples)
        steps.append(_step("resource_benchmark", "pass" if result["passed"] else "fail", **result))
    else:
        steps.append(_step("resource_benchmark", "not_run",
                           reason="Linux host and --benchmark-seconds (900 per case) required; run alongside Home Platform on the Pi"))

    by_name = {s["step"]: s for s in steps}
    blocking = next((n for n in REQUIRED_STEPS if by_name.get(n, {}).get("status") != "pass"), None)
    report = {
        "gate": "camera-engine-feasibility", "result": "passed" if blocking is None else "not_passed",
        "smallest_failing_step": blocking, "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {"system": platform.system(), "machine": platform.machine(), "node_is_target": False},
        "clip_spec": spec.__dict__, "steps": steps,
    }
    (out_dir / "gate-report.json").write_text(json.dumps(report, indent=2, default=str))
    return report
