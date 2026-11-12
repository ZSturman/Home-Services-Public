import pytest

import workspace_util.workspace_state as ws_mod
from workspace_util.config import Config, DiskConfig, TestWorkspaceConfig, WorkspaceConfig
from workspace_util.mount_safety import MountStatus
from workspace_util.workspace_state import (
    TestWorkspaceTooLargeError,
    WorkspaceMode,
    build_storage_contract,
    ensure_test_workspace_within_bounds,
    resolve_workspace_state,
    measure_test_workspace_bytes,
)


def _minimal_config(tmp_path, **overrides) -> Config:
    workspace = overrides.pop("workspace", None) or WorkspaceConfig(
        local_mac_path=str(tmp_path / "mac"),
        pi_path=str(tmp_path / "production-workspace"),
    )
    disk = overrides.pop("disk", None) or DiskConfig()
    from workspace_util.config import (
        BackupConfig, DashboardConfig, LoggingConfig, MacBridgeConfig,
        NotificationsConfig, SyncConfig,
    )
    return Config(
        workspace=workspace, disk=disk, sync=SyncConfig(), backup=BackupConfig(),
        dashboard=DashboardConfig(), mac_bridge=MacBridgeConfig(),
        notifications=NotificationsConfig(), logging=LoggingConfig(),
        commands={}, source_path=tmp_path / "config.yaml",
    )


def test_production_mode_when_uuid_set_and_mounted(tmp_path, monkeypatch):
    disk = DiskConfig(workspace_mount=str(tmp_path / "mnt"), workspace_uuid="abc-123")
    cfg = _minimal_config(tmp_path, disk=disk)
    monkeypatch.setattr(ws_mod, "check_mount", lambda *a, **k: MountStatus(path="x", expected=True, mounted=True))

    state = resolve_workspace_state(cfg)
    assert state.mode is WorkspaceMode.PRODUCTION
    assert state.active_path == cfg.workspace.pi_path
    assert state.is_active is True


def test_pending_when_uuid_configured_but_not_mounted_is_distinguishable(tmp_path, monkeypatch):
    """A drive that WAS configured going missing is different from never
    having been configured -- callers (doctor) key severity off this."""
    disk = DiskConfig(workspace_mount=str(tmp_path / "mnt"), workspace_uuid="abc-123")
    cfg = _minimal_config(tmp_path, disk=disk)
    monkeypatch.setattr(ws_mod, "check_mount", lambda *a, **k: MountStatus(path="x", expected=True, mounted=False, reason="not a mount point"))

    state = resolve_workspace_state(cfg)
    assert state.mode is WorkspaceMode.PENDING
    assert state.active_path is None
    assert "not currently mounted" in state.detail


def test_pending_when_nothing_configured_at_all(tmp_path):
    cfg = _minimal_config(tmp_path)  # no workspace_uuid, test mode off by default
    state = resolve_workspace_state(cfg)
    assert state.mode is WorkspaceMode.PENDING
    assert state.is_active is False
    assert "pending" in state.detail.lower() or "not configured" in state.detail.lower() or "No workspace" in state.detail


def test_test_mode_active_within_bounds(tmp_path):
    workspace = WorkspaceConfig(
        local_mac_path=str(tmp_path / "mac"),
        pi_path=str(tmp_path / "production-workspace"),
        test_workspace=TestWorkspaceConfig(enabled=True, path=str(tmp_path / "test-ws"), max_bytes=1000),
    )
    cfg = _minimal_config(tmp_path, workspace=workspace)

    state = resolve_workspace_state(cfg)
    assert state.mode is WorkspaceMode.TEST
    assert state.active_path == str(tmp_path / "test-ws")
    assert (tmp_path / "test-ws").is_dir()  # created on demand


def test_test_mode_falls_back_to_pending_when_over_size_cap(tmp_path):
    test_path = tmp_path / "test-ws"
    test_path.mkdir()
    (test_path / "big.bin").write_bytes(b"x" * 2000)

    workspace = WorkspaceConfig(
        local_mac_path=str(tmp_path / "mac"),
        pi_path=str(tmp_path / "production-workspace"),
        test_workspace=TestWorkspaceConfig(enabled=True, path=str(test_path), max_bytes=1000),
    )
    cfg = _minimal_config(tmp_path, workspace=workspace)

    state = resolve_workspace_state(cfg)
    assert state.mode is WorkspaceMode.PENDING
    assert "over its configured" in state.detail


def test_production_takes_priority_over_test_mode(tmp_path, monkeypatch):
    """The moment a real disk is configured and mounted, it wins over test
    mode automatically -- no other config flag needs to change."""
    disk = DiskConfig(workspace_mount=str(tmp_path / "mnt"), workspace_uuid="abc-123")
    workspace = WorkspaceConfig(
        local_mac_path=str(tmp_path / "mac"),
        pi_path=str(tmp_path / "production-workspace"),
        test_workspace=TestWorkspaceConfig(enabled=True, path=str(tmp_path / "test-ws")),
    )
    cfg = _minimal_config(tmp_path, workspace=workspace, disk=disk)
    monkeypatch.setattr(ws_mod, "check_mount", lambda *a, **k: MountStatus(path="x", expected=True, mounted=True))

    state = resolve_workspace_state(cfg)
    assert state.mode is WorkspaceMode.PRODUCTION
    assert not (tmp_path / "test-ws").exists()  # never even touched


def test_ensure_test_workspace_within_bounds_raises_over_cap(tmp_path):
    test_path = tmp_path / "test-ws"
    test_path.mkdir()
    (test_path / "big.bin").write_bytes(b"x" * 2000)
    workspace = WorkspaceConfig(
        local_mac_path=str(tmp_path / "mac"), pi_path=str(tmp_path / "prod"),
        test_workspace=TestWorkspaceConfig(enabled=True, path=str(test_path), max_bytes=1000),
    )
    cfg = _minimal_config(tmp_path, workspace=workspace)
    with pytest.raises(TestWorkspaceTooLargeError):
        ensure_test_workspace_within_bounds(cfg)


def test_measure_test_workspace_bytes_sums_nested_files(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_bytes(b"x" * 100)
    (tmp_path / "sub" / "b.txt").write_bytes(b"y" * 250)
    assert measure_test_workspace_bytes(str(tmp_path)) == 350


def test_measure_test_workspace_bytes_missing_dir_is_zero(tmp_path):
    assert measure_test_workspace_bytes(str(tmp_path / "does-not-exist")) == 0


def test_storage_contract_pending_is_not_safe_for_project_creation(tmp_path, monkeypatch):
    """The exact property Project Hub is meant to rely on: PENDING must never
    look safe to write into, and must not be mistaken for an error either
    (no UUID was ever configured -- see doctor.py's identical distinction)."""
    cfg = _minimal_config(tmp_path)
    monkeypatch.setattr(ws_mod, "check_mount", lambda *a, **k: MountStatus(path="x", expected=True, mounted=False, reason="not a mount point"))
    contract = build_storage_contract(cfg)
    assert contract.mode == "pending"
    assert contract.production_sync_safe is False
    assert contract.project_directory_creation_allowed is False
    assert contract.workspace_root is None
    assert contract.storage_health == "warning"  # never configured -- not a regression


def test_storage_contract_test_mode_is_never_mistaken_for_production(tmp_path):
    workspace = WorkspaceConfig(
        local_mac_path=str(tmp_path / "mac"),
        pi_path=str(tmp_path / "production-workspace"),
        test_workspace=TestWorkspaceConfig(enabled=True, path=str(tmp_path / "test-ws"), max_bytes=1000),
    )
    cfg = _minimal_config(tmp_path, workspace=workspace)
    contract = build_storage_contract(cfg)
    assert contract.mode == "test"
    assert contract.production_sync_safe is False
    assert contract.project_directory_creation_allowed is False
    assert contract.workspace_root is None  # only set for production, even though TEST has an active_path


def test_storage_contract_production_when_disk_mounted(tmp_path, monkeypatch):
    disk = DiskConfig(workspace_mount=str(tmp_path / "mnt"), workspace_uuid="abc-123")
    cfg = _minimal_config(tmp_path, disk=disk)
    monkeypatch.setattr(ws_mod, "check_mount", lambda *a, **k: MountStatus(path="x", expected=True, mounted=True))
    contract = build_storage_contract(cfg)
    assert contract.mode == "production"
    assert contract.production_sync_safe is True
    assert contract.workspace_disk_mounted is True
    assert contract.project_directory_creation_allowed is True
    assert contract.workspace_root == cfg.workspace.pi_path


def test_storage_contract_regression_disk_missing_is_error_health(tmp_path, monkeypatch):
    disk = DiskConfig(workspace_mount=str(tmp_path / "mnt"), workspace_uuid="abc-123")
    cfg = _minimal_config(tmp_path, disk=disk)
    monkeypatch.setattr(ws_mod, "check_mount", lambda *a, **k: MountStatus(path="x", expected=True, mounted=False, reason="unplugged"))
    contract = build_storage_contract(cfg)
    assert contract.mode == "pending"
    assert contract.storage_health == "error"
    assert contract.project_directory_creation_allowed is False
