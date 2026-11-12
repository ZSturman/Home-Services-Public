#!/usr/bin/env python3
"""Read-only retirement acceptance on the configured Pi (no secrets printed).

Run as kiosk, with /etc/home-services/compat/secrets.env exported, using
the compatibility venv. Also run the core doctor and observe report.
"""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from workspace_util.config import load_config
from workspace_util.net import resolve_bind_host


def check(name, condition):
    if not condition:
        raise RuntimeError(f"FAILED: {name}")
    print(f"ok: {name}")


def get(url, token=None):
    headers = {"X-Workspace-Token": token} if token else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=headers), timeout=15) as response:
        return json.load(response)


c = load_config("/etc/home-services/compat/config.yaml")
host = resolve_bind_host(c.dashboard.bind_host)
token = os.environ[c.dashboard.auth_token_env_var]
base = f"http://{host}:{c.dashboard.port}"
check("compatibility dashboard health", get(base + "/api/health")["status"] == "ok")
check("authenticated camera producer freshness", get(base + "/api/camera/live/status", token).get("available"))
check("Pi-to-Mac bridge health", get(base + "/api/mac/status", token)["status"] == "ok")
capabilities = get(base + "/api/mac/capabilities", token)
check("retired Mac backup trigger unavailable", capabilities.get("backup_available") is False)
check("recovery filesystem still mounted", os.path.ismount("/srv/workspace-backups"))
check("recovery snapshot tree readable", bool(list(Path("/srv/workspace-backups/snapshots").iterdir())))
check("test workspace disabled", not c.workspace.test_workspace.enabled)
check("competing camera capture disabled", not c.camera.background_capture_enabled)
units = [f"home-services-{x}" for x in (
    "gateway", "notifier", "worker@storage", "worker@backup", "worker@camera",
    "camera-stream", "compat-dashboard", "backup-browser",
    "backup-retention.timer", "backup-check.timer", "backup-disk-guard.timer", "network-guard.timer",
)]
check("all owned services/timers active", subprocess.run(["systemctl", "is-active", "--quiet", *units]).returncode == 0)
check("predecessor live services/timers inactive", not any(
    subprocess.run(["systemctl", "is-active", "--quiet", "workspace-" + name]).returncode == 0
    for name in ("dashboard", "camera-stream", "backup-browser", "backup.timer", "backup-retention.timer", "backup-check.timer", "network-guard.timer")
))
hp = Path("/home/service-user/home-platform/home-platform-next")
endpoint = next(line.split("=", 1)[1] for line in (hp / ".env").read_text().splitlines() if line.startswith("WORKSPACE_CONTRACT_URL="))
check("Home Platform targets core Home Services", endpoint.startswith("https://") and ":8443/api/storage/contract" in endpoint)
hp_token = (hp / "secrets/workspace_contract_token").read_text().strip()
check("Home Platform authenticated storage contract", "production_sync_safe" in get(endpoint, hp_token))
for path in ("/api/backup/status", "/api/backup/destination"):
    check("Home Platform authenticated " + path, "state" in get(endpoint.removesuffix("/api/storage/contract") + path, hp_token))
latest = max(Path(c.camera.live_stream_dir).glob("*.ts"), key=lambda p: p.stat().st_mtime)
check("camera segments younger than 10 seconds", time.time() - latest.stat().st_mtime < 10)
print("Read-only acceptance passed; no file mutation, backup, prune, automation command or hardware test performed.")
