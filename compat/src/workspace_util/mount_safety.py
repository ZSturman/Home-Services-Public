"""Mount safety checks.

This module exists for one reason: never let a 200GB+ sync or backup job
silently land on the Pi's SD card because an external drive isn't mounted.
Every write path (sync enable, backup run) MUST go through `require_mount`
or `check_mount` before touching disk.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class MountSafetyError(RuntimeError):
    """Raised when a required external mount is absent. Callers must treat
    this as fatal for the operation in progress -- never catch-and-fallback
    to writing at the same path on the root filesystem."""


@dataclass
class MountStatus:
    path: str
    expected: bool
    mounted: bool
    reason: str = ""
    device: str | None = None
    fstype: str | None = None

    @property
    def ok(self) -> bool:
        return (not self.expected) or self.mounted


def _read_proc_mounts() -> list[tuple[str, str, str]]:
    """Return (device, mountpoint, fstype) tuples from /proc/mounts (Linux)."""
    entries = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    entries.append((parts[0], parts[1], parts[2]))
    except FileNotFoundError:
        pass
    return entries


def _is_mountpoint_macos(path: Path) -> tuple[bool, str | None, str | None]:
    import subprocess
    try:
        out = subprocess.run(["diskutil", "info", str(path)], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return False, None, None
        device = None
        fstype = None
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.startswith("Device Node:"):
                device = line.split(":", 1)[1].strip()
            if line.startswith("Type (Bundle):") or line.startswith("File System Personality:"):
                fstype = line.split(":", 1)[1].strip()
        # diskutil info succeeds for the root volume too, so also verify the
        # path is actually a distinct mount, not just a directory under /.
        is_mp = os.path.ismount(str(path))
        return is_mp, device, fstype
    except Exception:
        return os.path.ismount(str(path)), None, None


def check_mount(path: str, expected: bool = True, required_uuid: str | None = None, _sysname: str | None = None) -> MountStatus:
    """Check whether `path` is a real, currently-mounted filesystem (not just
    an existing directory on the root filesystem).

    If `required_uuid` is set (Linux only), also verifies the mounted device's
    UUID matches -- so a *different* drive happening to be mounted at the same
    path doesn't pass silently. `_sysname` is test-only, to exercise both
    platform branches from either OS.
    """
    p = Path(path)

    if not p.exists():
        return MountStatus(path=path, expected=expected, mounted=False, reason="path does not exist")

    sysname = _sysname or os.uname().sysname
    if sysname == "Darwin":
        mounted, device, fstype = _is_mountpoint_macos(p)
        if not mounted:
            return MountStatus(path=path, expected=expected, mounted=False, reason="not a mount point")
        return MountStatus(path=path, expected=expected, mounted=True, device=device, fstype=fstype)

    # Linux: a path only counts as "mounted" if it's an actual mount point
    # (distinct device from its parent), matching a /proc/mounts entry.
    if not os.path.ismount(str(p)):
        return MountStatus(path=path, expected=expected, mounted=False, reason="not a mount point (path exists on root filesystem)")

    device = None
    fstype = None
    for dev, mp, fs in _read_proc_mounts():
        if os.path.realpath(mp) == os.path.realpath(str(p)):
            device, fstype = dev, fs
            break

    if required_uuid:
        actual_uuid = uuid_for_device(device) if device else None
        if actual_uuid != required_uuid:
            return MountStatus(
                path=path, expected=expected, mounted=False, device=device, fstype=fstype,
                reason=f"mounted device UUID ({actual_uuid}) does not match configured UUID ({required_uuid})",
            )

    return MountStatus(path=path, expected=expected, mounted=True, device=device, fstype=fstype)


def uuid_for_device(device: str, by_uuid_dir: str | Path = "/dev/disk/by-uuid") -> str | None:
    """Resolve a Linux device node (e.g. /dev/sda1) to its filesystem UUID via
    /dev/disk/by-uuid, without shelling out to blkid (which needs root)."""
    by_uuid_dir = Path(by_uuid_dir)
    if not by_uuid_dir.is_dir():
        return None
    try:
        target = os.path.realpath(device)
    except Exception:
        return None
    for entry in by_uuid_dir.iterdir():
        try:
            if os.path.realpath(str(entry)) == target:
                return entry.name
        except OSError:
            continue
    return None


def require_mount(path: str, required_uuid: str | None = None, what: str = "volume", _sysname: str | None = None) -> MountStatus:
    """Raise MountSafetyError if `path` is not a real mounted filesystem.

    This is the guard every sync-enable and backup-run code path must call
    before writing. It never creates the directory as a fallback.
    """
    status = check_mount(path, expected=True, required_uuid=required_uuid, _sysname=_sysname)
    if not status.ok:
        raise MountSafetyError(
            f"Expected {what} is not mounted at {path} ({status.reason}). "
            f"Refusing to write -- see docs/troubleshooting.md#missing-mount."
        )
    return status
