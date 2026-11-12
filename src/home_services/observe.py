"""Observation log: periodic health samples and a summary report.

`home-services observe record` (run every 15 minutes by
home-services-observe.timer) appends one JSON line to
<data_dir>/observation/<UTC date>.jsonl with the state of the Home Services
and predecessor units (active state, restart counts), doctor results,
module readiness, camera recording coverage for the interval, backup series
ages, the notification outbox, and host memory/load/disk/temperature.

`home-services observe report --hours 48` summarizes the samples and exits 1
if anything needs attention. Samples contain no paths, tokens, or secrets.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

HS_UNITS = ("home-services-gateway", "home-services-notifier", "home-services-worker@storage", "home-services-worker@backup",
            "home-services-worker@camera")
PREDECESSOR_UNITS = ("workspace-dashboard", "workspace-camera-stream", "workspace-backup-browser", "workspace-backup.timer",
                     "workspace-backup-retention.timer", "workspace-backup-check.timer")
COMPAT_UNITS = ("home-services-compat-dashboard", "home-services-camera-stream", "home-services-backup-browser",
                "home-services-backup-retention.timer", "home-services-backup-check.timer",
                "home-services-backup-disk-guard.timer", "home-services-network-guard.timer")
INTERVAL_SECONDS = 900
KEEP_DAYS = 30


def _unit(name: str) -> dict:
    try:
        out = subprocess.run(["systemctl", "show", name, "-p", "ActiveState", "-p", "NRestarts", "-p", "Result", "-p", "UnitFileState"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"active": "unknown"}
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"active": kv.get("ActiveState", "unknown"), "restarts": int(kv["NRestarts"]) if kv.get("NRestarts", "").isdigit() else None,
            "result": kv.get("Result"), "enabled": kv.get("UnitFileState")}


def _host() -> dict:
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            if k in ("MemAvailable", "SwapFree"):
                mem[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    try:
        load1, load5, _ = os.getloadavg()
    except OSError:
        load1 = load5 = None
    temp = None
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        pass
    return {"mem_available": mem.get("MemAvailable"), "swap_free": mem.get("SwapFree"), "load1": load1, "load5": load5,
            "temp_c": temp, "root_free": shutil.disk_usage("/").free}


def record(config_path: str) -> dict:
    from .cli import doctor_checks
    from .config import load_config
    from .services import build_services

    now = time.time()
    sample: dict = {"ts": now, "at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat().replace("+00:00", "Z")}
    sample["units"] = {u: _unit(u) for u in HS_UNITS}
    if Path("/etc/home-services/compat/ACTIVE").exists():
        sample["compatibility"] = {u: _unit(u) for u in COMPAT_UNITS}
    else:
        sample["predecessor"] = {u: _unit(u) for u in PREDECESSOR_UNITS}
        sample["predecessor"]["last_backup"] = _unit("workspace-backup.service")
    checks = doctor_checks(config_path)
    sample["doctor"] = {c["check"]: c["status"] for c in checks if c["status"] != "ok"}
    sample["host"] = _host()
    config = load_config(config_path)
    s = build_services(config)
    try:
        sample["readiness"] = {k: v.get("state") for k, v in s.module_readiness().items()}
        for v in config.volumes:
            st = s.guard.status(v)
            if st.verified:
                sample["host"][f"free:{v.id}"] = st.free_bytes
        cams = {}
        for d in config.cameras.devices:
            st = s.cameras.status(d.id) if s.cameras.engine_active else {"state": "not_managed"}
            row = s.db.one("SELECT COALESCE(SUM(MIN(start_ts + duration, ?) - MAX(start_ts, ?)), 0) AS covered FROM camera_chunks"
                           " WHERE camera_id=? AND start_ts < ? AND start_ts + duration > ?",
                           (now, now - INTERVAL_SECONDS, d.id, now, now - INTERVAL_SECONDS))
            events = s.db.one("SELECT COUNT(*) AS n FROM camera_events WHERE camera_id=? AND started_at > ?", (d.id, now - INTERVAL_SECONDS))
            cams[d.id] = {"state": st["state"], "covered_seconds": round(row["covered"] or 0, 1), "motion_events": events["n"]}
        sample["cameras"] = cams
        try:
            sample["backups"] = [{"host": g["hostname"], "tags": g["tags"], "state": g["protection"]["state"],
                                  "age_hours": round(g["protection"]["age_seconds"] / 3600, 2), "empty": g["protection"].get("empty"),
                                  "retired": bool(g.get("retired"))}
                                 for g in s.backups.groups()]
        except Exception as exc:  # noqa: BLE001 - the sample records the failure
            sample["backups"] = {"error": type(exc).__name__}
        sample["external_sources"] = [{"id": src["id"], "host": src["host"], "tag": src["tag"], "state": src["protection"]["state"],
                                       "age_hours": None if src["protection"].get("age_seconds") is None
                                       else round(src["protection"]["age_seconds"] / 3600, 2),
                                       "last_run": (src.get("last_run") or {}).get("state")}
                                      for src in s.backups.sources() if src.get("repository", "primary") != "primary"]
        sample["outbox"] = s.outbox.summary()
        jobs = s.db.one("SELECT COUNT(*) AS n FROM jobs WHERE state IN ('failed','interrupted') AND finished_at > ?", (now - INTERVAL_SECONDS,))
        sample["jobs_failed"] = jobs["n"]
    finally:
        s.db.close()
    out_dir = config.data_dir / "observation"
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / f"{sample['at'][:10]}.jsonl", "a") as f:
        f.write(json.dumps(sample, sort_keys=True) + "\n")
    for old in out_dir.glob("*.jsonl"):
        if now - old.stat().st_mtime > KEEP_DAYS * 86400:
            old.unlink()
    return sample


def load_samples(data_dir: Path, hours: float) -> list[dict]:
    cutoff = time.time() - hours * 3600
    samples = []
    for f in sorted((data_dir / "observation").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            if s.get("ts", 0) >= cutoff:
                samples.append(s)
    return samples


def summarize(samples: list[dict]) -> tuple[list[str], list[str]]:
    """(lines, problems) for a set of samples."""
    if not samples:
        return ["no samples"], ["no observation samples in the window"]
    problems: list[str] = []
    span = (samples[-1]["ts"] - samples[0]["ts"]) / 3600
    lines = [f"{len(samples)} samples over {span:.1f} h ({samples[0]['at']} .. {samples[-1]['at']})"]
    for group in ("units", "predecessor", "compatibility"):
        for name in sorted({name for s in samples for name in s.get(group, {})}):
            if name == "last_backup":
                continue
            states = [s[group][name]["active"] for s in samples if name in s.get(group, {})]
            restarts = [s[group][name].get("restarts") for s in samples if name in s.get(group, {})]
            restarts = [r for r in restarts if r is not None]
            up = sum(1 for x in states if x == "active") / len(states)
            grew = (max(restarts) - min(restarts)) if restarts else 0
            lines.append(f"{name}: active {up:.0%} of samples, restarts +{grew}")
            if up < 1 or grew:
                problems.append(f"{name} not always active ({up:.0%}) or restarted ({grew})")
    doctor_errors = [s["at"] for s in samples if any(v == "error" for v in s.get("doctor", {}).values())]
    lines.append(f"doctor errors in {len(doctor_errors)} samples")
    if doctor_errors:
        problems.append(f"doctor reported errors at {doctor_errors[0]} (+{len(doctor_errors) - 1} more)")
    for cam in samples[-1].get("cameras", {}):
        cov = [s["cameras"][cam]["covered_seconds"] for s in samples if cam in s.get("cameras", {})]
        low = [s["at"] for s in samples if cam in s.get("cameras", {}) and s["cameras"][cam]["covered_seconds"] < INTERVAL_SECONDS * 0.95]
        events = sum(s["cameras"][cam]["motion_events"] for s in samples if cam in s.get("cameras", {}))
        lines.append(f"camera {cam}: coverage min {min(cov) / INTERVAL_SECONDS:.0%}, {len(low)} intervals under 95%, {events} motion events")
        if len(low) > 1:  # the first sample after a restart or reboot can be short
            problems.append(f"camera {cam} recording coverage under 95% in {len(low)} intervals")
    mem = [s["host"]["mem_available"] for s in samples if s.get("host", {}).get("mem_available")]
    load = [s["host"]["load5"] for s in samples if s.get("host", {}).get("load5") is not None]
    temp = [s["host"]["temp_c"] for s in samples if s.get("host", {}).get("temp_c") is not None]
    if mem:
        lines.append(f"host: min MemAvailable {min(mem) / 1e9:.2f} GB, max load5 {max(load):.2f}" + (f", max temp {max(temp):.1f} C" if temp else ""))
        if min(mem) < 500_000_000 or max(load) >= 4:
            problems.append("host resources under the benchmark thresholds (MemAvailable < 0.5 GB or load5 >= 4)")
    failed = max((s.get("outbox", {}).get("failed", 0) for s in samples), default=0)
    jobs = sum(s.get("jobs_failed", 0) for s in samples)
    lines.append(f"notifications failed (max): {failed}; failed/interrupted jobs: {jobs}")
    if failed:
        problems.append(f"{failed} notification(s) failed delivery")
    last = samples[-1].get("backups")
    if isinstance(last, list):
        for g in last:
            lines.append(f"backup {g['host']} {','.join(g['tags'])}: {g['state']}, newest {g['age_hours']:.1f} h old" +
                         (", EMPTY" if g.get("empty") else "") + (" (retired)" if g.get("retired") else ""))
    else:
        problems.append(f"backup repository unreadable in the latest sample: {last}")
    for x in samples[-1].get("external_sources") or []:  # companion-reported, not verified by Home Services
        age = f", newest {x['age_hours']:.1f} h old" if x.get("age_hours") is not None else ""
        lines.append(f"external source {x.get('host', x['id'])} {x.get('tag', '')}: {x['state']}{age}, last run {x.get('last_run')}")
    return lines, problems
