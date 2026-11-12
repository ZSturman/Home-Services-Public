#!/usr/bin/env python3
"""Retire the old launch agent only after staging its independent replacement."""
import argparse
import os
import plistlib
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predecessor", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    root = here.parent.parent
    base = Path.home() / "Library/Application Support/HomeServices/MacBridge"
    release = base / "releases/20261025-retirement"
    label = "com.zacharysturman.homeservices.macbridge"
    agents = Path.home() / "Library/LaunchAgents"
    old_plist = agents / "com.workspace.macbridge.plist"
    new_plist = agents / f"{label}.plist"
    if not args.apply:
        print(f"Stage {root / 'compat'} into {release}, then replace com.workspace.macbridge with {label}.")
        return
    if not release.exists():
        subprocess.run([
            str(args.predecessor / ".venv/bin/python"), str(here / "install-runtime.py"),
            "--source", str(root / "compat"), "--destination", str(release),
        ], check=True)
    base.chmod(0o700)
    config = base / "config.yaml"
    secrets = base / "secrets.env"
    if config.exists() or secrets.exists() or new_plist.exists():
        parser.error("replacement configuration already exists; inspect instead of overwriting")
    config.write_text((args.predecessor / "config/config.yaml").read_text().replace(str(args.predecessor), str(base)))
    config.chmod(0o600)
    shutil.copy2(Path.home() / ".config/workspace-util/secrets.env", secrets)
    secrets.chmod(0o600)
    shutil.copy2(here / "run-mac-bridge.sh", base / "run-mac-bridge.sh")
    (base / "run-mac-bridge.sh").chmod(0o700)
    (base / "logs").mkdir(exist_ok=True)
    if not (base / "current").exists():
        (base / "current").symlink_to(release)
    agent = {
        "Label": label, "ProgramArguments": [str(base / "run-mac-bridge.sh")],
        "WorkingDirectory": str(base), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(base / "logs/macbridge.stdout.log"),
        "StandardErrorPath": str(base / "logs/macbridge.stderr.log"),
    }
    new_plist.write_bytes(plistlib.dumps(agent))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(old_plist)], check=True)
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(new_plist)], check=True)
        # Test the private bind selected by exactly the installed runtime.
        host = subprocess.check_output([
            str(release / "venv/bin/python"), "-c",
            "from workspace_util.net import resolve_bind_host; print(resolve_bind_host(''))",
        ], text=True).strip()
        port = subprocess.check_output([
            str(release / "venv/bin/python"), "-c",
            "from workspace_util.config import load_config; import sys; print(load_config(sys.argv[1]).mac_bridge.port)",
            str(config),
        ], text=True).strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(20):
            try:
                with opener.open(f"http://{host}:{port}/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("new Mac bridge health check failed")
    except BaseException:
        subprocess.run(["launchctl", "bootout", domain, str(new_plist)], check=False)
        subprocess.run(["launchctl", "bootstrap", domain, str(old_plist)], check=True)
        raise
    retired = Path.home() / "Library/LaunchAgents-retired"
    retired.mkdir(exist_ok=True)
    shutil.move(str(old_plist), retired / old_plist.name)
    print(f"{label}: healthy on private interface; old launch agent retired; no source-checkout dependency")


if __name__ == "__main__":
    main()
