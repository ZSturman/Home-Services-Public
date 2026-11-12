import os

import pytest

from workspace_util.mount_safety import (
    MountSafetyError,
    check_mount,
    require_mount,
    uuid_for_device,
)


def test_check_mount_path_does_not_exist(tmp_path):
    status = check_mount(str(tmp_path / "nope"))
    assert status.mounted is False
    assert "does not exist" in status.reason


def test_check_mount_linux_not_a_mountpoint(tmp_path, monkeypatch):
    # Path exists (it's just tmp_path itself) but is not a real mount point --
    # this is exactly the "someone mkdir'd the expected path" failure mode.
    monkeypatch.setattr(os.path, "ismount", lambda p: False)
    status = check_mount(str(tmp_path), _sysname="Linux")
    assert status.mounted is False
    assert status.ok is False
    assert "not a mount point" in status.reason


def test_check_mount_linux_real_mount_no_uuid_required(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    monkeypatch.setattr(
        "workspace_util.mount_safety._read_proc_mounts",
        lambda: [("/dev/sda1", str(tmp_path), "ext4")],
    )
    status = check_mount(str(tmp_path), _sysname="Linux")
    assert status.mounted is True
    assert status.device == "/dev/sda1"
    assert status.fstype == "ext4"


def test_check_mount_linux_uuid_mismatch_fails_closed(tmp_path, monkeypatch):
    """A different drive mounted at the expected path must NOT pass silently."""
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    monkeypatch.setattr(
        "workspace_util.mount_safety._read_proc_mounts",
        lambda: [("/dev/sdb1", str(tmp_path), "ext4")],
    )
    monkeypatch.setattr("workspace_util.mount_safety.uuid_for_device", lambda dev: "wrong-uuid")
    status = check_mount(str(tmp_path), required_uuid="expected-uuid", _sysname="Linux")
    assert status.mounted is False
    assert "does not match" in status.reason


def test_check_mount_linux_uuid_match_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    monkeypatch.setattr(
        "workspace_util.mount_safety._read_proc_mounts",
        lambda: [("/dev/sdb1", str(tmp_path), "ext4")],
    )
    monkeypatch.setattr("workspace_util.mount_safety.uuid_for_device", lambda dev: "expected-uuid")
    status = check_mount(str(tmp_path), required_uuid="expected-uuid", _sysname="Linux")
    assert status.mounted is True


def test_require_mount_raises_when_absent(tmp_path):
    with pytest.raises(MountSafetyError):
        require_mount(str(tmp_path / "absent"), what="workspace SSD")


def test_require_mount_never_creates_the_directory(tmp_path):
    """The core safety property: a missing mount must never be silently
    created as a plain directory and treated as success."""
    target = tmp_path / "mnt" / "workspace"
    with pytest.raises(MountSafetyError):
        require_mount(str(target))
    assert not target.exists()


def test_require_mount_passes_when_mounted(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    monkeypatch.setattr("workspace_util.mount_safety._read_proc_mounts", lambda: [])
    status = require_mount(str(tmp_path), what="test volume", _sysname="Linux")
    assert isinstance(status.ok, bool) and status.ok


def test_uuid_for_device_resolves_symlink(tmp_path):
    device = tmp_path / "sda1"
    device.write_text("")  # stand-in for a device node
    by_uuid_dir = tmp_path / "by-uuid"
    by_uuid_dir.mkdir()
    link = by_uuid_dir / "1234-5678"
    link.symlink_to(device)
    assert uuid_for_device(str(device), by_uuid_dir=by_uuid_dir) == "1234-5678"


def test_uuid_for_device_no_match_returns_none(tmp_path):
    by_uuid_dir = tmp_path / "by-uuid"
    by_uuid_dir.mkdir()
    assert uuid_for_device("/dev/sdz9", by_uuid_dir=by_uuid_dir) is None


def test_uuid_for_device_missing_by_uuid_dir_returns_none(tmp_path):
    assert uuid_for_device("/dev/sda1", by_uuid_dir=tmp_path / "does-not-exist") is None
