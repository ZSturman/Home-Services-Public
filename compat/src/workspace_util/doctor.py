"""`workspace doctor` -- a single aggregate health check.

Every check here is read-only and best-effort: a check that can't run (e.g.
Syncthing not installed yet) reports a warning, not a crash, so `doctor`
always finishes and gives a usable picture of the system.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .backup import BackupBlockedError, list_snapshots, repository_exists
from .config import Config
from .mount_safety import check_mount
from .workspace_state import WorkspaceMode, resolve_workspace_state


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warning" | "error"
    detail: str

    @property
    def symbol(self) -> str:
        return {"ok": "✓", "warning": "⚠", "error": "✗"}[self.status]


def _tailscale_check(cfg: Config) -> CheckResult:
    if not shutil.which("tailscale"):
        return CheckResult("Tailscale", "error", "tailscale not installed")
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return CheckResult("Tailscale", "error", "tailscale status failed -- is it logged in?")
        import json
        data = json.loads(out.stdout)
        if not data.get("Self", {}).get("Online", True):
            return CheckResult("Tailscale", "warning", "this device shows offline in tailscale status")
        return CheckResult("Tailscale", "ok", "connected")
    except Exception as exc:
        return CheckResult("Tailscale", "error", f"could not query tailscale: {exc}")


def _controller_check(cfg: Config) -> CheckResult:
    import platform
    label = "Pi/controller operational" if cfg.role == "pi" else "Mac agent operational"
    return CheckResult(label, "ok", platform.node())


def _workspace_disk_check(cfg: Config) -> CheckResult:
    state = resolve_workspace_state(cfg)
    if state.mode is WorkspaceMode.PRODUCTION:
        return CheckResult("Workspace SSD/NVMe", "ok", state.detail)
    if state.mode is WorkspaceMode.TEST:
        return CheckResult("Workspace SSD/NVMe", "warning", state.detail)
    # PENDING: a UUID that WAS configured but is now missing is a regression
    # (drive unplugged/failed) -- error. No UUID configured at all is the
    # expected, normal "no disk yet" state -- warning, not error.
    severity = "error" if cfg.disk.workspace_uuid else "warning"
    return CheckResult("Workspace SSD/NVMe", severity, state.detail)


def _workspace_path_check(cfg: Config) -> CheckResult:
    state = resolve_workspace_state(cfg)
    if not state.is_active:
        return CheckResult("Workspace path available", "warning", "no active workspace path (see Workspace SSD/NVMe check above)")
    p = Path(state.active_path)
    if not p.is_dir():
        return CheckResult("Workspace path available", "error", f"{p} does not exist")
    return CheckResult("Workspace path available", "ok", str(p))


def _syncthing_check(cfg: Config) -> CheckResult:
    if not shutil.which("syncthing"):
        return CheckResult("Syncthing running", "warning", "syncthing not installed yet")
    try:
        from .syncthing import SyncthingClient
        client = SyncthingClient.from_local_config()
        if client.ping():
            return CheckResult("Syncthing running", "ok", "responding on REST API")
        return CheckResult("Syncthing running", "error", "installed but not responding")
    except Exception as exc:
        return CheckResult("Syncthing running", "warning", str(exc))


def _mac_seen_check(cfg: Config) -> CheckResult:
    """Pi-side: is the Mac companion (mac_bridge) reachable. See
    _pi_seen_check for the Mac-side mirror of this question."""
    if not cfg.mac_bridge.enabled:
        return CheckResult("Mac companion", "warning", "mac_bridge disabled in config")
    try:
        import httpx
        r = httpx.get(f"http://{cfg.mac_bridge.host}:{cfg.mac_bridge.port}/health", timeout=5)
        if r.status_code == 200:
            return CheckResult("Mac seen", "ok", "responding now")
        return CheckResult("Mac seen", "warning", f"unexpected status {r.status_code}")
    except Exception as exc:
        return CheckResult("Mac seen", "warning", f"unreachable: {exc}")


def _pi_seen_check(cfg: Config) -> CheckResult:
    """Mac-side mirror of _mac_seen_check: is the Pi's dashboard reachable.
    Deliberately targets cfg.sync.pi_host (the Pi's own tailnet name), not
    net.resolve_bind_host, which would resolve *this* machine's Tailscale IP
    -- the mistake role-unaware doctor made before this split existed."""
    if not cfg.sync.pi_host:
        return CheckResult("Pi seen", "warning", "sync.pi_host not configured")
    try:
        import httpx
        r = httpx.get(f"http://{cfg.sync.pi_host}:{cfg.dashboard.port}/api/health", timeout=5)
        if r.status_code == 200:
            return CheckResult("Pi seen", "ok", "responding now")
        return CheckResult("Pi seen", "warning", f"unexpected status {r.status_code}")
    except Exception as exc:
        return CheckResult("Pi seen", "warning", f"unreachable: {exc}")


def _backup_disk_check(cfg: Config) -> CheckResult:
    status = check_mount(cfg.disk.backup_mount, required_uuid=cfg.disk.backup_uuid or None)
    if not status.mounted:
        return CheckResult("Backup HDD mounted", "error", status.reason or "not mounted")
    return CheckResult("Backup HDD mounted", "ok", cfg.disk.backup_mount)


def _backup_freshness_check(cfg: Config) -> CheckResult:
    if not repository_exists(cfg.backup, cfg.disk):
        return CheckResult("Last backup", "warning", "no backup repository initialized yet")
    try:
        snapshots = list_snapshots(cfg.backup, cfg.disk)
    except BackupBlockedError as exc:
        return CheckResult("Last backup", "error", str(exc))
    if not snapshots:
        return CheckResult("Last backup", "warning", "repository exists but has no snapshots yet")
    latest = max(snapshots, key=lambda s: s["time"])
    return CheckResult("Last backup", "ok", f"{latest['time']} ({latest['short_id']})")


def _backup_integrity_check(cfg: Config, log_dir: str) -> CheckResult:
    marker = Path(log_dir) / "last_integrity_check.json"
    if not marker.exists():
        return CheckResult("Backup integrity", "warning", "no integrity check has run yet")
    import json
    data = json.loads(marker.read_text())
    age_days = (time.time() - data["timestamp"]) / 86400
    status = "ok" if data.get("success") else "error"
    return CheckResult("Backup integrity", status, f"{'passed' if data.get('success') else 'FAILED'} {age_days:.1f}d ago")


def _disk_capacity_check(cfg: Config) -> CheckResult:
    # Regression: shutil.disk_usage() on an unmounted path silently reports
    # the *underlying* filesystem's usage (the Pi's SD card) instead of
    # failing -- caught live when the backup HDD was unmounted mid-test and
    # this check kept reporting a plausible-looking "16% full" the whole
    # time. Must check_mount first, not just call disk_usage and hope.
    mount = check_mount(cfg.disk.backup_mount, required_uuid=cfg.disk.backup_uuid or None)
    if not mount.mounted:
        return CheckResult("Backup disk capacity", "warning", f"backup disk not mounted ({mount.reason}) -- capacity unknown")
    try:
        usage = shutil.disk_usage(cfg.disk.backup_mount)
    except FileNotFoundError:
        return CheckResult("Backup disk capacity", "warning", "backup mount not present")
    pct_used = usage.used / usage.total * 100
    status = "warning" if pct_used > 80 else "ok"
    return CheckResult("Backup disk capacity", status, f"{pct_used:.0f}% full")


def _dashboard_check(cfg: Config) -> CheckResult:
    if not cfg.dashboard.enabled:
        return CheckResult("Dashboard healthy", "warning", "dashboard disabled in config")
    try:
        import httpx

        from .net import resolve_bind_host
        host = resolve_bind_host(cfg.dashboard.bind_host)
        r = httpx.get(f"http://{host}:{cfg.dashboard.port}/api/health", timeout=5)
        if r.status_code == 200:
            return CheckResult("Dashboard healthy", "ok", "responding")
        return CheckResult("Dashboard healthy", "error", f"status {r.status_code}")
    except Exception as exc:
        return CheckResult("Dashboard healthy", "error", f"unreachable: {exc}")


def _git_project_scan_check(cfg: Config) -> CheckResult:
    from .projects import discover_projects
    state = resolve_workspace_state(cfg)
    if not state.is_active:
        return CheckResult("Git project scan", "warning", "no active workspace path -- nothing to scan yet")
    root = Path(state.active_path) / cfg.workspace.projects_subdir
    if not root.is_dir():
        return CheckResult("Git project scan", "warning", f"{root} not present")
    projects = discover_projects(root)
    dirty = sum(1 for p in projects if p.git.is_repo and not p.git.clean)
    return CheckResult("Git project scan", "ok", f"{len(projects)} projects, {dirty} with local changes")


def _backup_integrity_check_for_cfg(cfg: Config) -> CheckResult:
    """Adapter so this check fits the uniform check(cfg) calling convention
    every other check uses -- it's the only one that also needs log_dir."""
    return _backup_integrity_check(cfg, cfg.logging.dir)


# Checks meaningful on both Mac and Pi.
COMMON_CHECKS = [
    _controller_check,
    _tailscale_check,
    _syncthing_check,
]

# Pi-only: physical backup HDD, workspace disk/path, the dashboard process,
# and project scanning against the Pi's active workspace path all only
# exist/apply on the Pi. "Is the Mac seen" is asked from the Pi's side.
PI_ONLY_CHECKS = [
    _workspace_disk_check,
    _workspace_path_check,
    _mac_seen_check,
    _backup_disk_check,
    _backup_freshness_check,
    _backup_integrity_check_for_cfg,
    _disk_capacity_check,
    _dashboard_check,
    _git_project_scan_check,
]

# Mac-only: the mirror question, "is the Pi seen", asked from the Mac's side.
MAC_ONLY_CHECKS = [
    _pi_seen_check,
]

PI_CHECKS = COMMON_CHECKS + PI_ONLY_CHECKS
MAC_CHECKS = COMMON_CHECKS + MAC_ONLY_CHECKS

# Every check that exists, for calling-convention sanity tests -- not what
# actually runs for a given role (see _checks_for_role/run_doctor).
CHECKS = PI_CHECKS + MAC_ONLY_CHECKS


def _checks_for_role(role: str) -> list:
    return PI_CHECKS if role == "pi" else MAC_CHECKS


def run_doctor(cfg: Config) -> list[CheckResult]:
    results = []
    for check in _checks_for_role(cfg.role):
        try:
            results.append(check(cfg))
        except Exception as exc:
            results.append(CheckResult(check.__name__, "error", f"check crashed: {exc}"))
    return results


def overall_status(results: list[CheckResult]) -> str:
    if any(r.status == "error" for r in results):
        return "UNHEALTHY"
    if any(r.status == "warning" for r in results):
        return "HEALTHY WITH WARNINGS"
    return "HEALTHY"


def format_report(results: list[CheckResult]) -> str:
    lines = ["Workspace Doctor", ""]
    for r in results:
        lines.append(f"{r.symbol} {r.name}: {r.detail}")
    lines.append("")
    lines.append(f"Overall: {overall_status(results)}")
    return "\n".join(lines)
