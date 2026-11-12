import json
from pathlib import Path

import pytest

import workspace_util.backup as backup_mod
from workspace_util.config import BackupConfig, DiskConfig
from workspace_util.mount_safety import MountSafetyError, MountStatus


def _cfgs(tmp_path):
    disk_cfg = DiskConfig(workspace_mount=str(tmp_path / "ws"), backup_mount=str(tmp_path / "backup"))
    backup_cfg = BackupConfig(destination=str(tmp_path / "backup"), repository_subdir="restic-repo")
    return backup_cfg, disk_cfg


def test_backup_blocked_when_disk_not_mounted(tmp_path, monkeypatch):
    """The critical fail-safe: if the backup HDD isn't mounted, no restic
    command may run and no fallback write may happen on the SD card."""
    backup_cfg, disk_cfg = _cfgs(tmp_path)

    def fake_require_mount(path, required_uuid=None, what="volume"):
        raise MountSafetyError(f"{what} not mounted at {path}")

    monkeypatch.setattr(backup_mod, "require_mount", fake_require_mount)

    with pytest.raises(backup_mod.BackupBlockedError, match="BACKUP BLOCKED"):
        backup_mod.ensure_backup_disk_mounted(disk_cfg)

    # And the destination directory must never have been created as a side effect.
    assert not (tmp_path / "backup").exists()


def test_run_backup_returns_failure_result_when_disk_missing(tmp_path, monkeypatch):
    backup_cfg, disk_cfg = _cfgs(tmp_path)

    def fake_require_mount(path, required_uuid=None, what="volume"):
        raise MountSafetyError("nope")

    monkeypatch.setattr(backup_mod, "require_mount", fake_require_mount)

    result = backup_mod.run_backup(str(tmp_path / "ws"), backup_cfg, disk_cfg)
    assert result.success is False
    assert "BACKUP BLOCKED" in result.error


def test_run_backup_never_calls_restic_when_disk_missing(tmp_path, monkeypatch):
    backup_cfg, disk_cfg = _cfgs(tmp_path)
    monkeypatch.setattr(backup_mod, "require_mount", lambda *a, **k: (_ for _ in ()).throw(MountSafetyError("nope")))

    called = {"restic": False}
    monkeypatch.setattr(backup_mod.subprocess, "run", lambda *a, **k: called.__setitem__("restic", True))

    backup_mod.run_backup(str(tmp_path / "ws"), backup_cfg, disk_cfg)
    assert called["restic"] is False


def _pass_mount(monkeypatch):
    monkeypatch.setattr(backup_mod, "require_mount", lambda *a, **k: MountStatus(path="x", expected=True, mounted=True))


def test_run_backup_parses_restic_summary(tmp_path, monkeypatch):
    backup_cfg, disk_cfg = _cfgs(tmp_path)
    _pass_mount(monkeypatch)
    monkeypatch.setenv("WORKSPACE_BACKUP_PASSWORD", "test-password")

    summary_line = json.dumps({
        "message_type": "summary", "snapshot_id": "abc123",
        "data_added": 4096, "files_new": 2, "files_changed": 1,
    })

    class FakeResult:
        returncode = 0
        stdout = summary_line + "\n"
        stderr = ""

    monkeypatch.setattr(backup_mod.subprocess, "run", lambda *a, **k: FakeResult())

    result = backup_mod.run_backup(str(tmp_path / "ws"), backup_cfg, disk_cfg)
    assert result.success is True
    assert result.snapshot_id == "abc123"
    assert result.data_added_bytes == 4096
    assert result.files_new == 2


def test_run_backup_missing_password_env_blocks(tmp_path, monkeypatch):
    backup_cfg, disk_cfg = _cfgs(tmp_path)
    _pass_mount(monkeypatch)
    monkeypatch.delenv("WORKSPACE_BACKUP_PASSWORD", raising=False)

    result = backup_mod.run_backup(str(tmp_path / "ws"), backup_cfg, disk_cfg)
    assert result.success is False
    assert "password" in result.error.lower()


def test_backup_status_marks_abandoned_active_run_stale(tmp_path):
    status_path = backup_mod.backup_status_path(str(tmp_path))
    status_path.write_text(json.dumps({"state": "running", "active": True, "stage": "backing_up", "updated_at": 1}))
    status = backup_mod.read_backup_status(str(tmp_path), now=1 + backup_mod.ACTIVE_STATUS_MAX_AGE_SECONDS + 1)
    assert status["state"] == "interrupted"
    assert status["active"] is False
    assert "deadline" in status["stale_reason"]


def test_apply_retention_uses_configured_keep_counts(tmp_path, monkeypatch):
    backup_cfg = BackupConfig(
        destination=str(tmp_path / "backup"), hourly_keep=24, daily_keep=14, weekly_keep=8, monthly_keep=12,
    )
    disk_cfg = DiskConfig(backup_mount=str(tmp_path / "backup"))
    _pass_mount(monkeypatch)
    monkeypatch.setenv("WORKSPACE_BACKUP_PASSWORD", "test-password")

    captured = {}

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, env, capture_output, text, timeout):
        captured["argv"] = argv
        return FakeResult()

    monkeypatch.setattr(backup_mod.subprocess, "run", fake_run)
    backup_mod.apply_retention(backup_cfg, disk_cfg)

    argv = captured["argv"]
    assert argv[:2] == ["restic", "forget"]
    assert "--keep-hourly" in argv and argv[argv.index("--keep-hourly") + 1] == "24"
    assert "--keep-daily" in argv and argv[argv.index("--keep-daily") + 1] == "14"
    assert "--keep-weekly" in argv and argv[argv.index("--keep-weekly") + 1] == "8"
    assert "--keep-monthly" in argv and argv[argv.index("--keep-monthly") + 1] == "12"
    assert "--prune" in argv


def test_apply_retention_blocked_without_mount(tmp_path, monkeypatch):
    backup_cfg, disk_cfg = _cfgs(tmp_path)
    monkeypatch.setattr(backup_mod, "require_mount", lambda *a, **k: (_ for _ in ()).throw(MountSafetyError("nope")))
    with pytest.raises(backup_mod.BackupBlockedError):
        backup_mod.apply_retention(backup_cfg, disk_cfg)


def test_restore_snapshot_writes_to_dated_subdir_not_live_workspace(tmp_path, monkeypatch):
    backup_cfg, disk_cfg = _cfgs(tmp_path)
    _pass_mount(monkeypatch)
    monkeypatch.setenv("WORKSPACE_BACKUP_PASSWORD", "test-password")

    captured = {}

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, env, capture_output, text, timeout):
        captured["argv"] = argv
        return FakeResult()

    monkeypatch.setattr(backup_mod.subprocess, "run", fake_run)

    restore_root = tmp_path / "restores"
    live_workspace = tmp_path / "ws"
    live_workspace.mkdir()

    backup_mod.restore_snapshot("abc123", backup_cfg, disk_cfg, str(restore_root))

    argv = captured["argv"]
    target = argv[argv.index("--target") + 1]
    assert target.startswith(str(restore_root))
    assert target != str(live_workspace)
    # The dated restore directory must actually have been created ahead of restic running.
    assert Path(target).is_dir()


def test_restore_snapshot_blocked_without_mount(tmp_path, monkeypatch):
    backup_cfg, disk_cfg = _cfgs(tmp_path)
    monkeypatch.setattr(backup_mod, "require_mount", lambda *a, **k: (_ for _ in ()).throw(MountSafetyError("nope")))
    with pytest.raises(backup_mod.BackupBlockedError):
        backup_mod.restore_snapshot("abc123", backup_cfg, disk_cfg, str(tmp_path / "restores"))


def test_snapshot_browser_lists_only_direct_children_and_uses_fixed_restic_argv(tmp_path, monkeypatch):
    backup_cfg, disk_cfg = _cfgs(tmp_path)
    _pass_mount(monkeypatch)
    monkeypatch.setenv("WORKSPACE_BACKUP_PASSWORD", "test-password")
    captured = {}

    class FakeResult:
        returncode = 0
        stderr = ""
        stdout = "\n".join([
            json.dumps({"struct_type": "node", "name": "Projects", "path": "/Projects", "type": "dir"}),
            json.dumps({"struct_type": "node", "name": "README.md", "path": "/README.md", "type": "file", "size": 12}),
            json.dumps({"struct_type": "node", "name": "nested.txt", "path": "/Projects/nested.txt", "type": "file", "size": 6}),
        ])

    def fake_restic(args, repo, timeout):
        captured.update(args=args, repo=repo, timeout=timeout)
        return FakeResult()

    monkeypatch.setattr(backup_mod, "_run_restic", fake_restic)
    entries = backup_mod.list_snapshot_directory("abcdef12", "/", backup_cfg, disk_cfg)

    assert [item["name"] for item in entries] == ["Projects", "README.md"]
    assert captured["args"] == ["ls", "--json", "abcdef12", "--", "/"]


def test_snapshot_browser_rejects_selector_and_traversal_before_running_restic(tmp_path, monkeypatch):
    backup_cfg, disk_cfg = _cfgs(tmp_path)
    called = {"restic": False}
    monkeypatch.setattr(backup_mod, "_run_restic", lambda *args: called.__setitem__("restic", True))

    with pytest.raises(backup_mod.BackupBlockedError, match="snapshot ID"):
        backup_mod.list_snapshot_directory("latest", "/", backup_cfg, disk_cfg)
    with pytest.raises(backup_mod.BackupBlockedError, match="cannot contain"):
        backup_mod.list_snapshot_directory("abcdef12", "/Projects/../secret", backup_cfg, disk_cfg)
    assert called["restic"] is False
