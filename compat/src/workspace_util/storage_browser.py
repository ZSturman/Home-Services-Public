"""Read-only, mount-gated browsing of the attached backup HDD.

The dashboard deliberately treats the backup mount as one bounded storage
root.  Browser input is always a relative path, hidden entries are omitted,
and resolving every candidate prevents both ``..`` traversal and symlink
escapes.  The paginated scanner keeps memory bounded even for very large
directories.
"""
from __future__ import annotations

import base64
import bisect
import json
import os
from pathlib import Path

from .config import Config
from .files import guess_mimetype
from .mount_safety import check_mount
from .paths import PathNotAllowedError, join_within_root

DEFAULT_PAGE_SIZE = 120
MAX_PAGE_SIZE = 200


class StorageUnavailableError(RuntimeError):
    """The configured HDD is absent, not its expected drive, or unreadable."""


def storage_root(cfg: Config) -> Path:
    """Return the one browser root only when its UUID-backed mount is live."""
    mount = check_mount(
        cfg.disk.backup_mount,
        required_uuid=cfg.disk.backup_uuid or None,
    )
    if not mount.mounted:
        raise StorageUnavailableError("The storage drive is not currently mounted.")
    root = Path(cfg.disk.backup_mount).resolve()
    if not root.is_dir():
        raise StorageUnavailableError("The storage drive is not available for browsing.")
    return root


def resolve_storage_path(cfg: Config, relative_path: str = "") -> Path:
    """Resolve one relative browser path under the verified HDD root."""
    root = storage_root(cfg)
    try:
        return join_within_root(str(root), relative_path or ".")
    except PathNotAllowedError:
        # Keep this error independent from host filesystem names and details.
        raise


def _cursor_key(cursor: str | None) -> tuple[int, str, str] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not (isinstance(value, list) and len(value) == 3):
            raise ValueError
        kind, folded, name = value
        if kind not in (0, 1) or not isinstance(folded, str) or not isinstance(name, str):
            raise ValueError
        return kind, folded, name
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid page cursor") from None


def _encode_cursor(key: tuple[int, str, str]) -> str:
    raw = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _entry_key(name: str, is_dir: bool) -> tuple[int, str, str]:
    # Folders first, then a stable case-insensitive order.  Preserve the
    # original name as a tie breaker for unusual case-sensitive filesystems.
    return (0 if is_dir else 1, name.casefold(), name)


def list_storage_directory(
    cfg: Config,
    relative_path: str = "",
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> dict:
    """Return one bounded page from the attached HDD.

    ``os.scandir`` avoids materializing the whole directory.  We retain only
    the first page worth of sort keys while scanning, so a directory with
    hundreds of thousands of entries cannot exhaust the dashboard's memory.
    """
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    after_key = _cursor_key(cursor)
    root = storage_root(cfg)
    directory = resolve_storage_path(cfg, relative_path)
    if not directory.is_dir():
        raise NotADirectoryError(str(relative_path or "/"))
    normalized_relative = directory.relative_to(root).as_posix()
    if normalized_relative == ".":
        normalized_relative = ""

    retained: list[tuple[tuple[int, str, str], dict]] = []
    try:
        with os.scandir(directory) as scanner:
            for item in scanner:
                if item.name.startswith("."):
                    continue
                try:
                    # Follow a symlink only after proving its real target is
                    # still inside the mount.  Escaping links are invisible.
                    target = Path(item.path).resolve()
                    target.relative_to(root)
                    stat = target.stat()
                    is_dir = target.is_dir()
                except (OSError, RuntimeError, ValueError):
                    continue
                key = _entry_key(item.name, is_dir)
                if after_key is not None and key <= after_key:
                    continue
                browser_path = (Path(normalized_relative) / item.name).as_posix()
                entry = {
                    "name": item.name,
                    "path": browser_path,
                    "is_dir": is_dir,
                    "size_bytes": None if is_dir else stat.st_size,
                    "modified": stat.st_mtime,
                    "type": "Folder" if is_dir else guess_mimetype(target),
                }
                position = bisect.bisect_left([candidate[0] for candidate in retained], key)
                retained.insert(position, (key, entry))
                if len(retained) > limit + 1:
                    retained.pop()
    except OSError as exc:
        raise StorageUnavailableError("The storage drive could not be read.") from exc

    has_more = len(retained) > limit
    page = retained[:limit]
    return {
        "path": relative_path.strip("/"),
        "entries": [entry for _, entry in page],
        "next_cursor": _encode_cursor(page[-1][0]) if has_more and page else None,
    }


def storage_status(cfg: Config) -> dict:
    """A compact, user-safe health summary for Home and System."""
    try:
        root = storage_root(cfg)
        usage = os.statvfs(root)
    except StorageUnavailableError as exc:
        return {"mounted": False, "detail": str(exc), "root": cfg.disk.backup_mount}
    return {
        "mounted": True,
        "detail": "Storage mounted",
        "root": str(root),
        "available_bytes": usage.f_bavail * usage.f_frsize,
        "total_bytes": usage.f_blocks * usage.f_frsize,
    }
