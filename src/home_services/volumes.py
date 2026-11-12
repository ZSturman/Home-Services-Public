"""Verified-volume guards.

A managed root is writable only when its volume is mounted at the configured
mount point *and* the mounted device carries the configured filesystem UUID.
A directory that merely exists (for example an empty mount point on the SD
card) is never accepted. Callers re-check before every write and throughout
long jobs; on failure they stop, and nothing falls back to another path.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import WRITABLE_ROLES, Config, VolumeConfig
from .errors import PathNotAllowed, VolumeUnavailable

# Filesystems that must not hold SQLite WAL databases (no reliable locking/
# shared memory semantics, and they live on removable media).
NON_DB_FILESYSTEMS = {"exfat", "vfat", "msdos", "fat", "ntfs", "ntfs3", "fuseblk", "cifs", "smb3", "nfs", "nfs4", "fuse"}


@dataclass(frozen=True)
class MountEntry:
    mount_point: str
    device: str
    fstype: str
    st_dev: int
    uuid: str | None


class MountProbe:
    def entry_for(self, mount_point: str) -> MountEntry | None:
        raise NotImplementedError

    def fstype_for_path(self, path: str) -> str | None:
        raise NotImplementedError


def _unescape_mountinfo(s: str) -> str:
    return s.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


class LinuxMountProbe(MountProbe):
    def __init__(self, mountinfo: str = "/proc/self/mountinfo", by_uuid: str = "/dev/disk/by-uuid"):
        self.mountinfo = mountinfo
        self.by_uuid = by_uuid

    def _entries(self) -> list[tuple[str, str, str, int, int]]:
        out = []
        try:
            lines = Path(self.mountinfo).read_text().splitlines()
        except OSError:
            return out
        for line in lines:
            left, _, right = line.partition(" - ")
            lp, rp = left.split(), right.split()
            if len(lp) < 5 or len(rp) < 2:
                continue
            major, minor = (int(x) for x in lp[2].split(":"))
            out.append((_unescape_mountinfo(lp[4]), rp[1], rp[0], major, minor))
        return out

    def _uuid_for(self, device: str) -> str | None:
        d = Path(self.by_uuid)
        if not d.is_dir():
            return None
        target = os.path.realpath(device)
        for entry in d.iterdir():
            if os.path.realpath(entry) == target:
                return entry.name
        return None

    def entry_for(self, mount_point: str) -> MountEntry | None:
        real = os.path.realpath(mount_point)
        match = None
        for mp, device, fstype, major, minor in self._entries():
            if mp == real:
                match = (mp, device, fstype, major, minor)  # last one wins (over-mounts)
        if match is None:
            return None
        mp, device, fstype, major, minor = match
        try:
            st_dev = os.stat(real).st_dev
        except OSError:
            return None
        if (os.major(st_dev), os.minor(st_dev)) != (major, minor):
            return None
        return MountEntry(mp, device, fstype, st_dev, self._uuid_for(device))

    def fstype_for_path(self, path: str) -> str | None:
        real = os.path.realpath(path)
        best = None
        for mp, _device, fstype, _maj, _min in self._entries():
            if real == mp or real.startswith(mp.rstrip("/") + "/"):
                if best is None or len(mp) > len(best[0]):
                    best = (mp, fstype)
        return best[1] if best else None


class FixtureMountProbe(MountProbe):
    """Development/test probe. A JSON table maps mount paths to the identity
    a real disk would report: {"<mount>": {"uuid", "fstype", "device"}}.
    Removing an entry simulates unplugging; changing the uuid simulates the
    wrong disk. The fixture directory's own st_dev stands in for the device."""

    def __init__(self, table: Path):
        self.table = table

    def _load(self) -> dict:
        try:
            return json.loads(self.table.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def entry_for(self, mount_point: str) -> MountEntry | None:
        real = os.path.realpath(mount_point)
        for mp, info in self._load().items():
            if os.path.realpath(mp) == real and Path(real).is_dir():
                return MountEntry(real, info.get("device", "fixture"), info.get("fstype", "fixture"), os.stat(real).st_dev, info.get("uuid"))
        return None

    def fstype_for_path(self, path: str) -> str | None:
        return self._load().get("__data_dir_fstype__", {}).get("fstype", "apfs")


def probe_for(config: Config) -> MountProbe:
    if config.mount_probe == "fixture":
        if config.mount_fixture_table is None:
            raise VolumeUnavailable("mounts.fixture_table is required for the fixture probe")
        return FixtureMountProbe(config.mount_fixture_table)
    return LinuxMountProbe()


@dataclass(frozen=True)
class VolumeStatus:
    volume_id: str
    mount: str
    state: str  # "verified" | "missing" | "wrong_volume" | "wrong_fstype"
    reason: str
    fstype: str | None
    total_bytes: int | None = None
    free_bytes: int | None = None
    unmounted_mountpoint_writable: bool | None = None

    @property
    def verified(self) -> bool:
        return self.state == "verified"

    def as_dict(self) -> dict:
        d = {
            "id": self.volume_id, "state": self.state, "reason": self.reason, "filesystem": self.fstype,
            "capacity": None if self.total_bytes is None else {"total_bytes": self.total_bytes, "free_bytes": self.free_bytes},
        }
        return d


class VolumeGuard:
    def __init__(self, config: Config, probe: MountProbe | None = None):
        self.config = config
        self.probe = probe or probe_for(config)

    def status(self, volume: VolumeConfig) -> VolumeStatus:
        entry = self.probe.entry_for(volume.mount)
        if entry is None:
            writable = None
            if os.path.isdir(volume.mount):
                writable = os.access(volume.mount, os.W_OK)
            return VolumeStatus(volume.id, volume.mount, "missing", "volume is not mounted at its configured mount point", None,
                                unmounted_mountpoint_writable=writable)
        if entry.uuid != volume.uuid:
            return VolumeStatus(volume.id, volume.mount, "wrong_volume",
                                f"mounted filesystem UUID {entry.uuid!r} does not match the configured volume", entry.fstype)
        if volume.fstype and entry.fstype != volume.fstype:
            return VolumeStatus(volume.id, volume.mount, "wrong_fstype",
                                f"mounted filesystem type {entry.fstype!r} does not match configured {volume.fstype!r}", entry.fstype)
        try:
            usage = shutil.disk_usage(volume.mount)
            total, free = usage.total, usage.free
        except OSError:
            total = free = None
        return VolumeStatus(volume.id, volume.mount, "verified", "mounted with the configured UUID", entry.fstype, total, free)

    def require(self, volume: VolumeConfig) -> tuple[VolumeStatus, int]:
        """Return (status, st_dev) for a verified volume or raise."""
        st = self.status(volume)
        if not st.verified:
            raise VolumeUnavailable(f"volume {volume.id!r} unavailable: {st.reason}",
                                    details={"volume_id": volume.id, "state": st.state})
        entry = self.probe.entry_for(volume.mount)
        assert entry is not None
        return st, entry.st_dev

    def root_path(self, role: str, *, write: bool) -> Path:
        """Verified absolute path of a managed root. Raises unless the volume
        is verified and the root directory exists on that same device (not a
        nested mount or a symlink pointing elsewhere)."""
        volume = self.config.volume_for_role(role)
        if volume is None:
            raise VolumeUnavailable(f"no volume is configured for the {role} root", details={"role": role, "state": "not_configured"})
        if write and role not in WRITABLE_ROLES:
            raise PathNotAllowed(f"the {role} root is read-only to Home Services")
        _st, st_dev = self.require(volume)
        root = Path(volume.mount) / volume.roots[role]
        try:
            real = root.resolve(strict=True)
            mount_real = Path(os.path.realpath(volume.mount))
            if real.stat().st_dev != st_dev or mount_real not in real.parents:
                raise VolumeUnavailable(f"{role} root is not on the verified volume", details={"role": role, "state": "root_off_volume"})
        except FileNotFoundError:
            raise VolumeUnavailable(
                f"{role} root does not exist on the verified volume; prepare it with `home-services volumes prepare`",
                details={"role": role, "state": "root_missing"},
            ) from None
        return real

    def recheck(self, role: str, expected_root: Path) -> None:
        """Mid-operation re-verification for long writes and jobs."""
        if self.root_path(role, write=role in WRITABLE_ROLES) != expected_root:
            raise VolumeUnavailable(f"{role} root changed during the operation", details={"role": role, "state": "changed"})

    def data_dir_filesystem_ok(self) -> tuple[bool, str | None]:
        fstype = self.probe.fstype_for_path(str(self.config.data_dir))
        # Unknown is refused: the state store must be on a known local filesystem.
        return (fstype is not None and fstype not in NON_DB_FILESYSTEMS, fstype)


def contained(root: Path, relative: str) -> Path:
    """Join a client-supplied relative path onto a verified root. Rejects
    absolute paths, `..`, NUL, and anything resolving outside the root
    (including through symlinks)."""
    if "\x00" in relative:
        raise PathNotAllowed("path contains NUL")
    if relative.startswith("/"):
        raise PathNotAllowed("path must be relative to the root")
    rel = Path(relative.rstrip("/")) if relative else Path()
    if rel.is_absolute() or ".." in rel.parts:
        raise PathNotAllowed("path must be relative and must not contain '..'")
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathNotAllowed("path resolves outside the root")
    return candidate
