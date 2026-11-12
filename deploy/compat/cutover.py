#!/usr/bin/env python3
"""Cut over already-staged Pi compatibility units; retain exact legacy state.

No backup, prune, repository deletion, or Wi-Fi reconnect is run here.
Run as root after installing and checking the new runtime/units/config.
"""
import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

OLD_TIMERS = [f"workspace-{x}.timer" for x in (
    "backup", "backup-retention", "backup-check", "backup-disk-guard", "network-guard",
)]
OLD_SERVICES = [f"workspace-{x}.service" for x in (
    "camera-stream", "dashboard", "backup-browser", "backup-disk-guard", "disk-guard",
)]
NEW_SERVICES = [f"home-services-{x}.service" for x in (
    "camera-stream", "compat-dashboard", "backup-browser", "backup-disk-guard",
)]
NEW_TIMERS = [f"home-services-{x}.timer" for x in (
    "backup-retention", "backup-check", "backup-disk-guard", "network-guard",
)]
STATE = Path("/var/lib/home-services/retirement/predecessor-units.json")
ACTIVE = Path("/etc/home-services/compat/ACTIVE")


def ctl(*args, check=True):
    return subprocess.run(["systemctl", *args], check=check, capture_output=True, text=True)


def restore(state):
    ctl("disable", "--now", *NEW_TIMERS, *NEW_SERVICES, check=False)
    ACTIVE.unlink(missing_ok=True)
    observer = Path("/etc/systemd/system/home-services-observe.service.d/20-owned-services.conf")
    if observer.exists():
        observer.rename(observer.with_suffix(".conf.disabled"))
    ctl("daemon-reload")
    for unit, previous in state.items():
        if previous["enabled"]:
            ctl("enable", unit)
    for unit, previous in state.items():
        if previous["active"]:
            ctl("start", unit)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.rollback:
        restore(json.loads(STATE.read_text()))
        print("restored predecessor unit enablement/running state; data/config unchanged")
        return
    if STATE.exists():
        parser.error("cutover state already exists; inspect it rather than overwriting rollback evidence")
    # Do not cut across a currently running maintenance writer/check.
    for suffix in ("backup", "backup-retention", "backup-check"):
        if ctl("is-active", "--quiet", f"workspace-{suffix}.service", check=False).returncode == 0:
            parser.error(f"workspace-{suffix} is running; wait for it to finish")
    state = {u: {
        "enabled": ctl("is-enabled", "--quiet", u, check=False).returncode == 0,
        "active": ctl("is-active", "--quiet", u, check=False).returncode == 0,
    } for u in OLD_TIMERS + OLD_SERVICES}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    try:
        ctl("disable", "--now", *OLD_TIMERS)
        for suffix in ("backup", "backup-retention", "backup-check"):
            if ctl("is-active", "--quiet", f"workspace-{suffix}.service", check=False).returncode == 0:
                raise RuntimeError(f"workspace-{suffix} started during cutover; retry after completion")
        ctl("disable", "--now", *OLD_SERVICES)
        ctl("reset-failed", *OLD_SERVICES, check=False)
        # Retain calendar history so Persistent=true cannot trigger pruning now.
        for suffix in ("backup-retention", "backup-check"):
            old = Path(f"/var/lib/systemd/timers/stamp-workspace-{suffix}.timer")
            new = old.with_name(f"stamp-home-services-{suffix}.timer")
            if not old.exists():
                raise RuntimeError(f"missing timer history: {old}; refusing immediate maintenance")
            shutil.copy2(old, new)
        ctl("enable", "--now", *NEW_SERVICES)
        # Fresh generated segments prove output, not just process startup.
        hls = Path("/run/workspace-camera-hls")
        for _ in range(30):
            if any(time.time() - p.stat().st_mtime < 10 for p in hls.glob("*.ts")):
                break
            time.sleep(1)
        else:
            raise RuntimeError("new camera producer emitted no fresh HLS segment")
        ctl("is-active", "--quiet", *NEW_SERVICES[:3])
        ctl("enable", "--now", *NEW_TIMERS)
        ACTIVE.touch()
        print("cutover complete; legacy units disabled; new producer/dashboard/recovery/timers active")
    except BaseException:
        restore(state)
        STATE.rename(STATE.with_suffix(".failed.json"))
        raise


if __name__ == "__main__":
    main()
