"""Disk discovery -- lists candidate disks without ever touching them.

Nothing in this module formats, partitions, or writes to a disk. It only
reports what's there, so `workspace disk list` is always safe to run.
"""
from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass


@dataclass
class DiskInfo:
    device: str
    model: str
    capacity_bytes: int
    filesystem: str | None
    mountpoint: str | None
    uuid: str | None
    used_bytes: int | None
    free_bytes: int | None
    is_internal: bool

    def human(self, n: int | None) -> str:
        if n is None:
            return "-"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f}{unit}"
            n /= 1024
        return f"{n:.1f}PB"


def _df_bytes(mountpoint: str) -> tuple[int | None, int | None]:
    try:
        out = subprocess.run(["df", "-k", mountpoint], capture_output=True, text=True, timeout=10)
        lines = out.stdout.strip().splitlines()
        if len(lines) < 2:
            return None, None
        fields = lines[-1].split()
        used_kb, avail_kb = int(fields[2]), int(fields[3])
        return used_kb * 1024, avail_kb * 1024
    except Exception:
        return None, None


def list_disks_linux() -> list[DiskInfo]:
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,MODEL,SIZE,FSTYPE,MOUNTPOINT,UUID,TYPE,TRAN"],
        capture_output=True, text=True, timeout=15,
    )
    data = json.loads(out.stdout or "{}")
    disks: list[DiskInfo] = []

    def walk(devices, is_internal_hint):
        for dev in devices:
            if dev.get("type") in ("disk", "part"):
                mountpoint = dev.get("mountpoint")
                used, free = (_df_bytes(mountpoint) if mountpoint else (None, None))
                tran = dev.get("tran") or ""
                is_internal = tran == "mmc" or is_internal_hint
                if dev.get("type") == "part" or (dev.get("type") == "disk" and not dev.get("children")):
                    disks.append(DiskInfo(
                        device=f"/dev/{dev['name']}",
                        model=(dev.get("model") or "").strip() or "unknown",
                        capacity_bytes=int(dev.get("size") or 0),
                        filesystem=dev.get("fstype"),
                        mountpoint=mountpoint,
                        uuid=dev.get("uuid"),
                        used_bytes=used,
                        free_bytes=free,
                        is_internal=is_internal,
                    ))
            if dev.get("children"):
                walk(dev["children"], tran == "mmc" if (tran := dev.get("tran")) else is_internal_hint)

    walk(data.get("blockdevices", []), False)
    return disks


def _macos_disk_info(identifier: str, fallback_model: str = "unknown", fallback_size: int = 0) -> "DiskInfo":
    import plistlib
    info_out = subprocess.run(["diskutil", "info", "-plist", identifier], capture_output=True, text=True, timeout=10)
    try:
        info = plistlib.loads(info_out.stdout.encode())
    except Exception:
        info = {}
    mountpoint = info.get("MountPoint") or None
    used, free = (_df_bytes(mountpoint) if mountpoint else (None, None))
    return DiskInfo(
        device=f"/dev/{identifier}",
        model=info.get("MediaName") or info.get("VolumeName") or fallback_model,
        capacity_bytes=int(info.get("TotalSize") or fallback_size or 0),
        filesystem=info.get("FilesystemType") or info.get("Content"),
        mountpoint=mountpoint,
        uuid=info.get("VolumeUUID") or info.get("DiskUUID"),
        used_bytes=used,
        free_bytes=free,
        is_internal=bool(info.get("Internal")),
    )


def list_disks_macos() -> list[DiskInfo]:
    """Lists real, individually mountable volumes -- including APFS volumes
    nested inside a container, which `diskutil list`'s top-level Partitions
    array does not include directly."""
    out = subprocess.run(["diskutil", "list", "-plist"], capture_output=True, text=True, timeout=15)
    import plistlib
    plist = plistlib.loads(out.stdout.encode())
    disks: list[DiskInfo] = []
    seen: set[str] = set()

    for entry in plist.get("AllDisksAndPartitions", []):
        apfs_volumes = entry.get("APFSVolumes", [])
        if apfs_volumes:
            for vol in apfs_volumes:
                identifier = vol.get("DeviceIdentifier")
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                disks.append(_macos_disk_info(identifier, vol.get("VolumeName", "unknown"), vol.get("Size", 0)))
            continue  # the container itself (e.g. disk3) isn't separately mountable

        partitions = entry.get("Partitions", [])
        if not partitions:
            identifier = entry.get("DeviceIdentifier")
            if identifier and identifier not in seen:
                seen.add(identifier)
                disks.append(_macos_disk_info(identifier, "unknown", entry.get("Size", 0)))
            continue

        for part in partitions:
            identifier = part.get("DeviceIdentifier")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            disks.append(_macos_disk_info(identifier, part.get("VolumeName", "unknown"), part.get("Size", 0)))

    return disks


def list_disks() -> list[DiskInfo]:
    system = platform.system()
    if system == "Linux":
        return list_disks_linux()
    if system == "Darwin":
        return list_disks_macos()
    raise NotImplementedError(f"Disk listing not implemented for {system}")


def list_candidate_external_disks() -> list[DiskInfo]:
    """Disks that are plausible candidates for the workspace/backup role:
    not internal, has a filesystem, has meaningful capacity."""
    return [d for d in list_disks() if not d.is_internal and d.capacity_bytes > 1_000_000_000]
