"""Managed ordinary-file storage: browsing, directories, managed Trash.

Only the `files` root is browsable/writable as ordinary files. Trash and
upload staging live under the `staging` root, which is enforced to be on the
same volume so publish/trash/restore are atomic renames.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .config import Config
from .db import Database
from .errors import Conflict, InsufficientSpace, NotFound, PathNotAllowed
from .runtime import Clock, IdFactory, iso
from .volumes import VolumeGuard, contained

ACTIVE_UPLOAD_STATES = ("open", "verifying")


def keep_both_name(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(name)
    n = 1
    while True:
        candidate = directory / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
    return total


class Storage:
    def __init__(self, config: Config, db: Database, guard: VolumeGuard, clock: Clock, ids: IdFactory):
        self.config, self.db, self.guard, self.clock, self.ids = config, db, guard, clock, ids

    # --- roots ------------------------------------------------------------

    def files_root(self, *, write: bool) -> Path:
        return self.guard.root_path("files", write=write)

    def staging_area(self, area: str) -> Path:
        root = self.guard.root_path("staging", write=True)
        d = root / area
        d.mkdir(exist_ok=True)  # inside a verified root only
        return d

    def _rel(self, root: Path, p: Path) -> str:
        return "" if p == root else str(p.relative_to(root))

    # --- space accounting -------------------------------------------------

    def outstanding_reservations(self) -> int:
        row = self.db.one(
            "SELECT COALESCE(SUM(size - offset), 0) AS r FROM uploads WHERE state IN (%s)" % ",".join("?" * len(ACTIVE_UPLOAD_STATES)),
            ACTIVE_UPLOAD_STATES,
        )
        return int(row["r"])

    def staging_bytes(self) -> int:
        row = self.db.one(
            "SELECT COALESCE(SUM(size), 0) AS r FROM uploads WHERE state IN (%s)" % ",".join("?" * len(ACTIVE_UPLOAD_STATES)),
            ACTIVE_UPLOAD_STATES,
        )
        return int(row["r"])

    def capacity(self) -> dict:
        volume = self.config.volume_for_role("files")
        st, _ = self.guard.require(volume)
        reserved = self.outstanding_reservations()
        reserve = self.config.limits.volume_reserve_bytes
        return {
            "volume_id": volume.id, "total_bytes": st.total_bytes, "free_bytes": st.free_bytes,
            "volume_reserve_bytes": reserve, "reserved_by_uploads_bytes": reserved,
            "available_for_uploads_bytes": max(0, (st.free_bytes or 0) - reserve - reserved),
            "staging_cap_bytes": self.config.limits.staging_cap_bytes, "staging_used_bytes": self.staging_bytes(),
        }

    def ensure_space(self, new_bytes: int) -> None:
        cap = self.capacity()
        if new_bytes > cap["available_for_uploads_bytes"]:
            raise InsufficientSpace("not enough space after the volume reserve and outstanding uploads", details={
                "requested_bytes": new_bytes, "available_bytes": cap["available_for_uploads_bytes"],
                "volume_reserve_bytes": cap["volume_reserve_bytes"]})
        if cap["staging_used_bytes"] + new_bytes > cap["staging_cap_bytes"]:
            raise InsufficientSpace("the aggregate staging cap would be exceeded", code="staging_cap_exceeded", details={
                "requested_bytes": new_bytes, "staging_used_bytes": cap["staging_used_bytes"], "staging_cap_bytes": cap["staging_cap_bytes"]})

    # --- browsing ---------------------------------------------------------

    def _entry(self, root: Path, p: Path, st: os.stat_result) -> dict:
        is_dir = stat.S_ISDIR(st.st_mode)
        return {"name": p.name, "path": self._rel(root, p), "kind": "directory" if is_dir else "file",
                "size": None if is_dir else st.st_size, "modified_at": iso(st.st_mtime)}

    def list(self, path: str, *, limit: int = 200, cursor: str | None = None) -> dict:
        root = self.files_root(write=False)
        target = contained(root, path)
        if not target.is_dir():
            raise NotFound("directory not found", details={"path": path})
        entries = []
        for child in sorted(target.iterdir(), key=lambda c: c.name):
            try:
                st = child.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                continue  # links and special files are never exposed
            entries.append(self._entry(root, child, st))
        start = 0
        if cursor:
            start = next((i for i, e in enumerate(entries) if e["name"] > cursor), len(entries))
        limit = max(1, min(limit, 1000))
        page = entries[start:start + limit]
        next_cursor = page[-1]["name"] if start + limit < len(entries) and page else None
        return {"root": "files", "path": self._rel(root, target), "entries": page, "next_cursor": next_cursor}

    def entry(self, path: str) -> dict:
        """One entry's metadata (a file or directory), without listing its parent."""
        root = self.files_root(write=False)
        target = contained(root, path)
        try:
            st = target.lstat()
        except FileNotFoundError:
            raise NotFound("entry not found", details={"path": path}) from None
        if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            raise NotFound("entry not found", details={"path": path})
        return self._entry(root, target, st)

    def file_for_download(self, path: str) -> Path:
        root = self.files_root(write=False)
        target = contained(root, path)
        if not target.is_file() or target.is_symlink():
            raise NotFound("file not found", details={"path": path})
        return target

    def mkdir(self, path: str) -> dict:
        root = self.files_root(write=True)
        target = contained(root, path)
        if target == root:
            raise PathNotAllowed("cannot create the root")
        if target.exists():
            raise Conflict("an entry already exists at that path", code="already_exists", details={"path": path})
        if not target.parent.is_dir():
            raise NotFound("parent directory not found", details={"path": self._rel(root, target.parent)})
        target.mkdir()
        return self._entry(root, target, target.stat())

    def move(self, source: str, destination: str, *, conflict: str = "fail") -> dict:
        """Rename or move an entry within the files root (a same-volume
        rename). Case-only renames are allowed on case-insensitive volumes
        such as exFAT."""
        if conflict not in ("fail", "keep_both"):
            raise PathNotAllowed("conflict must be fail or keep_both")
        root = self.files_root(write=True)
        src = contained(root, source)
        dest = contained(root, destination)
        if root in (src, dest):
            raise PathNotAllowed("cannot move the root or onto the root")
        if not src.exists() or src.is_symlink():
            raise NotFound("entry not found", details={"path": source})
        if src.is_dir() and src in dest.parents:
            raise PathNotAllowed("cannot move a directory inside itself")
        if not dest.parent.is_dir():
            raise NotFound("destination directory not found", code="parent_missing", details={"path": self._rel(root, dest.parent)})
        same_entry = dest.exists() and os.path.samefile(src, dest)
        if dest.exists() and not same_entry:
            if conflict == "fail":
                raise Conflict("an entry already exists at the destination", code="destination_exists",
                               details={"path": self._rel(root, dest)})
            dest = keep_both_name(dest.parent, dest.name)
        self.guard.recheck("files", root)
        if same_entry and src.name != dest.name:
            # Case-only rename on a case-insensitive volume: Linux treats both
            # names as one dentry and renames nothing, so go through a temporary name.
            hop = src.with_name(f".hs-rename-{self.ids.new('mv')}")
            os.rename(src, hop)
            os.rename(hop, dest)
        else:
            os.rename(src, dest)
        return {"from": self._rel(root, src), **self._entry(root, dest, dest.stat())}

    # --- managed Trash ----------------------------------------------------

    def trash(self, path: str, principal_id: str | None) -> dict:
        root = self.files_root(write=True)
        target = contained(root, path)
        if target == root:
            raise PathNotAllowed("cannot trash the root")
        if not target.exists() or target.is_symlink():
            raise NotFound("entry not found", details={"path": path})
        trash_dir = self.staging_area("trash")
        trash_id = self.ids.new("trs")
        is_dir = target.is_dir()
        size = _tree_size(target)
        self.guard.recheck("files", root)
        os.rename(target, trash_dir / trash_id)
        now = self.clock.now()
        self.db.execute(
            "INSERT INTO trash (id, root, original_path, trash_name, is_dir, size, deleted_at, deleted_by) VALUES (?,?,?,?,?,?,?,?)",
            (trash_id, "files", self._rel(root, target), trash_id, int(is_dir), size, now, principal_id),
        )
        return self.trash_item(trash_id)

    def trash_item(self, trash_id: str) -> dict:
        row = self.db.one("SELECT * FROM trash WHERE id=?", (trash_id,))
        if row is None:
            raise NotFound("trash item not found")
        purge_at = row["deleted_at"] + self.config.limits.trash_retention_days * 86400
        return {"id": row["id"], "original_path": row["original_path"], "kind": "directory" if row["is_dir"] else "file",
                "size": row["size"], "deleted_at": iso(row["deleted_at"]), "purge_after": iso(purge_at)}

    def list_trash(self) -> list[dict]:
        return [self.trash_item(r["id"]) for r in self.db.all("SELECT id FROM trash ORDER BY deleted_at DESC, id DESC")]

    def restore(self, trash_id: str) -> dict:
        item = self.trash_item(trash_id)
        root = self.files_root(write=True)
        source = self.staging_area("trash") / trash_id
        if not source.exists():
            raise NotFound("trashed data is missing from the staging volume", code="trash_data_missing")
        original = contained(root, item["original_path"])
        if not original.parent.is_dir():
            raise Conflict("the original parent directory no longer exists", code="parent_missing",
                           details={"path": item["original_path"]})
        dest = keep_both_name(original.parent, original.name)
        self.guard.recheck("files", root)
        os.rename(source, dest)
        self.db.execute("DELETE FROM trash WHERE id=?", (trash_id,))
        return {"restored_path": self._rel(root, dest), "renamed": dest != original}

    def purge_expired_trash(self) -> int:
        """Permanently delete Trash entries older than the retention window."""
        cutoff = self.clock.now() - self.config.limits.trash_retention_days * 86400
        trash_dir = self.staging_area("trash")
        purged = 0
        for row in self.db.all("SELECT id FROM trash WHERE deleted_at < ?", (cutoff,)):
            p = trash_dir / row["id"]
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif p.exists() or p.is_symlink():
                p.unlink()
            self.db.execute("DELETE FROM trash WHERE id=?", (row["id"],))
            purged += 1
        return purged
