"""Regression coverage for `workspace doctor`.

The most important property here isn't any single check's verdict (those
depend on what's actually installed on the machine running the tests) --
it's that run_doctor() never crashes on a calling-convention mismatch. A
real deployment caught exactly this: _tailscale_check() was defined with no
parameters while run_doctor() calls every check as check(cfg) uniformly,
which surfaced as "check crashed: _tailscale_check() takes 0 positional
arguments but 1 was given" only once actually invoked live.
"""
from __future__ import annotations

from workspace_util.config import (
    BackupConfig, Config, DashboardConfig, DiskConfig, LoggingConfig,
    MacBridgeConfig, NotificationsConfig, SyncConfig, WorkspaceConfig,
)
from workspace_util.doctor import CHECKS, MAC_CHECKS, PI_CHECKS, overall_status, run_doctor


def _cfg(tmp_path, role: str = "pi") -> Config:
    return Config(
        workspace=WorkspaceConfig(local_mac_path=str(tmp_path / "mac"), pi_path=str(tmp_path / "pi-workspace")),
        disk=DiskConfig(workspace_mount=str(tmp_path / "mnt-ws"), backup_mount=str(tmp_path / "mnt-backup")),
        sync=SyncConfig(),
        backup=BackupConfig(destination=str(tmp_path / "mnt-backup")),
        dashboard=DashboardConfig(bind_host="127.0.0.1"),
        mac_bridge=MacBridgeConfig(enabled=False),
        notifications=NotificationsConfig(),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
        commands={},
        source_path=tmp_path / "config.yaml",
        role=role,
    )


def test_run_doctor_never_crashes_on_calling_convention(tmp_path):
    """No check may raise a TypeError from a bad call signature -- every
    entry in CHECKS is invoked identically as check(cfg) by run_doctor."""
    results = run_doctor(_cfg(tmp_path))
    for r in results:
        assert "crashed" not in r.detail, f"{r.name} crashed: {r.detail}"


def test_every_registered_check_is_callable_with_one_config_arg(tmp_path):
    """Direct unit-level check of the same property, independent of
    run_doctor's try/except swallowing exceptions into a CheckResult."""
    cfg = _cfg(tmp_path)
    for check in CHECKS:
        check(cfg)  # must not raise TypeError


def test_run_doctor_returns_one_result_per_pi_check(tmp_path):
    results = run_doctor(_cfg(tmp_path, role="pi"))
    assert len(results) == len(PI_CHECKS)


def test_run_doctor_returns_one_result_per_mac_check(tmp_path):
    results = run_doctor(_cfg(tmp_path, role="mac"))
    assert len(results) == len(MAC_CHECKS)


def test_mac_role_skips_pi_only_checks(tmp_path):
    """The headline property of the role split: a Mac doesn't have a backup
    HDD, a workspace SSD/NVMe, or a dashboard process, so `doctor` run on the
    Mac must not report on any of them -- see docs/troubleshooting.md's
    former "lots of X on the Mac" complaint."""
    results = run_doctor(_cfg(tmp_path, role="mac"))
    names = {r.name for r in results}
    assert "Workspace SSD/NVMe" not in names
    assert "Backup HDD mounted" not in names
    assert "Dashboard healthy" not in names
    assert "Mac agent operational" in names
    assert "Pi seen" in names


def test_pi_role_keeps_original_checks(tmp_path):
    results = run_doctor(_cfg(tmp_path, role="pi"))
    names = {r.name for r in results}
    assert "Workspace SSD/NVMe" in names
    assert "Backup HDD mounted" in names
    assert "Pi/controller operational" in names
    assert "Pi seen" not in names


def test_missing_workspace_disk_is_warning_not_error_when_never_configured(tmp_path):
    """The headline behavior this session's work is about: no SSD/NVMe
    configured must not read as a fatal/error condition."""
    cfg = _cfg(tmp_path)
    assert cfg.disk.workspace_uuid == ""
    results = run_doctor(cfg)
    workspace_check = next(r for r in results if r.name == "Workspace SSD/NVMe")
    assert workspace_check.status == "warning"
    assert overall_status(results) != "UNHEALTHY" or any(
        r.status == "error" for r in results if r.name != "Workspace SSD/NVMe"
    )


def test_disk_capacity_check_does_not_report_sd_card_usage_when_backup_unmounted(tmp_path, monkeypatch):
    """Regression: shutil.disk_usage() on an unmounted directory silently
    reports the underlying filesystem's usage instead of erroring. Caught
    live -- the check kept showing a plausible "16% full" for the Pi's SD
    card the entire time the backup HDD was actually unplugged."""
    import workspace_util.doctor as doctor_mod
    from workspace_util.mount_safety import MountStatus

    cfg = _cfg(tmp_path)
    cfg.disk.backup_mount = str(tmp_path)  # tmp_path exists on the test's real filesystem
    monkeypatch.setattr(doctor_mod, "check_mount", lambda *a, **k: MountStatus(path="x", expected=True, mounted=False, reason="not a mount point"))

    result = doctor_mod._disk_capacity_check(cfg)
    assert result.status == "warning"
    assert "not mounted" in result.detail
    assert "full" not in result.detail  # must not report a bogus percentage


def test_disk_capacity_check_reports_usage_when_actually_mounted(tmp_path, monkeypatch):
    import workspace_util.doctor as doctor_mod
    from workspace_util.mount_safety import MountStatus

    cfg = _cfg(tmp_path)
    cfg.disk.backup_mount = str(tmp_path)
    monkeypatch.setattr(doctor_mod, "check_mount", lambda *a, **k: MountStatus(path="x", expected=True, mounted=True))

    result = doctor_mod._disk_capacity_check(cfg)
    assert "full" in result.detail


def test_dashboard_check_resolves_blank_bind_host_instead_of_building_bad_url(tmp_path, monkeypatch):
    """Regression: with bind_host="" (the documented "auto-detect the
    Tailscale IP" default), the dashboard check must not literally build
    http://:PORT/... -- it must resolve through net.resolve_bind_host like
    dashboard/serve.py does, the same way the real server binds."""
    import workspace_util.doctor as doctor_mod
    import workspace_util.net as net_mod

    cfg = _cfg(tmp_path)
    cfg.dashboard.bind_host = ""
    # _dashboard_check does a local `from .net import resolve_bind_host`, so
    # the patch target is the source module, not doctor_mod's namespace.
    monkeypatch.setattr(net_mod, "resolve_bind_host", lambda configured: "127.0.0.1")

    captured = {}

    class FakeResponse:
        status_code = 200

    def fake_get(url, timeout):
        captured["url"] = url
        return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    result = doctor_mod._dashboard_check(cfg)
    assert result.status == "ok"
    assert captured["url"].startswith("http://127.0.0.1:") or captured["url"].startswith(f"http://{cfg.dashboard.bind_host or '127.0.0.1'}")
    assert "http://:" not in captured["url"]


def test_missing_workspace_disk_with_configured_uuid_is_error(tmp_path, monkeypatch):
    """A drive that WAS configured but disappeared is a real regression --
    that must stay an error, distinct from the "never configured" warning."""
    import workspace_util.workspace_state as ws_mod
    cfg = _cfg(tmp_path)
    cfg.disk.workspace_uuid = "some-uuid-that-is-now-missing"
    monkeypatch.setattr(ws_mod, "check_mount", lambda *a, **k: __import__(
        "workspace_util.mount_safety", fromlist=["MountStatus"]
    ).MountStatus(path="x", expected=True, mounted=False, reason="not a mount point"))
    results = run_doctor(cfg)
    workspace_check = next(r for r in results if r.name == "Workspace SSD/NVMe")
    assert workspace_check.status == "error"
