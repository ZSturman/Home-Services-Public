"""Resumable upload sessions.

A session reserves space, accepts bytes strictly at the server-acknowledged
offset, verifies size and SHA-256 at completion, and publishes by a
same-volume rename into the files root. The database offset is authoritative:
bytes past it (a crash between write and commit) are truncated on resume.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path

from .config import Config
from .db import Database
from .errors import Conflict, NotFound, PathNotAllowed, ServiceError
from .runtime import Clock, IdFactory, iso
from .storage import Storage, keep_both_name
from .volumes import contained

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_publish_lock = threading.Lock()


class Transfers:
    def __init__(self, config: Config, db: Database, storage: Storage, clock: Clock, ids: IdFactory):
        self.config, self.db, self.storage, self.clock, self.ids = config, db, storage, clock, ids

    def _row(self, upload_id: str):
        row = self.db.one("SELECT * FROM uploads WHERE id=?", (upload_id,))
        if row is None:
            raise NotFound("upload session not found", details={"upload_id": upload_id})
        return row

    def view(self, upload_id: str) -> dict:
        r = self._row(upload_id)
        expires = r["updated_at"] + self.config.limits.staging_abandon_hours * 3600
        return {
            "id": r["id"], "root": r["root"], "path": r["path"], "size": r["size"], "sha256": r["sha256"],
            "conflict": r["conflict"], "state": r["state"], "offset": r["offset"],
            "published_path": r["published_path"], "error": None if r["error"] is None else {"code": r["error"]},
            "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"]),
            "abandon_after": iso(expires) if r["state"] == "open" else None,
        }

    def _partial(self, upload_id: str) -> Path:
        return self.storage.staging_area("uploads") / f"{upload_id}.part"

    def create(self, *, root: str, path: str, size: int, sha256: str | None, conflict: str,
               idempotency_key: str | None, principal_id: str | None) -> tuple[dict, bool]:
        if root != "files":
            raise PathNotAllowed("uploads are accepted only into the files root", details={"root": root})
        if size < 0:
            raise ServiceError("size must be non-negative", code="invalid_size")
        if sha256 is not None and not _SHA256.match(sha256):
            raise ServiceError("sha256 must be 64 lowercase hex characters", code="invalid_sha256")
        if conflict not in ("keep_both", "fail"):
            raise ServiceError("conflict must be keep_both or fail", code="invalid_conflict")
        files_root = self.storage.files_root(write=True)
        target = contained(files_root, path)
        if target == files_root or not target.name:
            raise PathNotAllowed("path must name a file")
        rel = str(target.relative_to(files_root))
        if idempotency_key:
            row = self.db.one("SELECT * FROM uploads WHERE idempotency_key=?", (idempotency_key,))
            if row is not None:
                if (row["path"], row["size"], row["sha256"]) != (rel, size, sha256):
                    raise Conflict("idempotency key was already used for a different upload", code="idempotency_mismatch",
                                   details={"upload_id": row["id"]})
                return self.view(row["id"]), False
        with self.db.tx():
            self.storage.ensure_space(size)
            upload_id = self.ids.new("upl")
            now = self.clock.now()
            self.db.execute(
                "INSERT INTO uploads (id, root, path, size, sha256, conflict, state, offset, staging_name, idempotency_key,"
                " created_at, updated_at, created_by) VALUES (?,?,?,?,?,?,'open',0,?,?,?,?,?)",
                (upload_id, root, rel, size, sha256, conflict, f"{upload_id}.part", idempotency_key, now, now, principal_id),
            )
        self._partial(upload_id).touch(exist_ok=False)
        return self.view(upload_id), True

    def append(self, upload_id: str, offset: int, chunks) -> dict:
        """Append an iterable of byte chunks at `offset`."""
        row = self._row(upload_id)
        if row["state"] != "open":
            raise Conflict(f"upload is {row['state']}", code="upload_not_open")
        partial = self._partial(upload_id)
        if not partial.exists():
            self._fail(upload_id, "partial_missing")
            raise Conflict("the partial file is missing from staging; restart this file", code="partial_missing")
        acknowledged = row["offset"]
        actual = partial.stat().st_size
        if actual != acknowledged:
            acknowledged = min(actual, acknowledged)
            with open(partial, "r+b") as f:
                f.truncate(acknowledged)
            self.db.execute("UPDATE uploads SET offset=? WHERE id=?", (acknowledged, upload_id))
        if offset != acknowledged:
            raise Conflict("offset does not match the server's acknowledged offset", code="offset_mismatch",
                           details={"expected_offset": acknowledged})
        staging_root = self.storage.guard.root_path("staging", write=True)
        written = 0
        limit = self.config.limits.max_chunk_bytes
        with open(partial, "r+b") as f:
            f.seek(offset)
            for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > limit:
                    f.truncate(offset)
                    raise ServiceError(f"chunk exceeds {limit} bytes", code="chunk_too_large", status=413)
                if offset + written > row["size"]:
                    f.truncate(offset)
                    raise ServiceError("data exceeds the declared size", code="size_exceeded", status=413)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        # The volume must still be the verified one after the write.
        self.storage.guard.recheck("staging", staging_root)
        self.db.execute("UPDATE uploads SET offset=?, updated_at=? WHERE id=? AND state='open'",
                        (offset + written, self.clock.now(), upload_id))
        return self.view(upload_id)

    def complete(self, upload_id: str, *, create_parents: bool = False) -> dict:
        row = self._row(upload_id)
        if row["state"] == "published":
            return self.view(upload_id)  # idempotent replay
        if row["state"] != "open":
            raise Conflict(f"upload is {row['state']}", code="upload_not_open")
        if row["offset"] != row["size"]:
            raise Conflict("upload is incomplete", code="upload_incomplete", details={"offset": row["offset"], "size": row["size"]})
        partial = self._partial(upload_id)
        self.db.execute("UPDATE uploads SET state='verifying', updated_at=? WHERE id=?", (self.clock.now(), upload_id))
        h = hashlib.sha256()
        size = 0
        with open(partial, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
                size += len(block)
        digest = h.hexdigest()
        if size != row["size"]:
            self._fail(upload_id, "size_mismatch")
            raise Conflict("staged size does not match the declared size", code="size_mismatch")
        if row["sha256"] and digest != row["sha256"]:
            self._fail(upload_id, "checksum_mismatch")
            partial.unlink(missing_ok=True)
            raise Conflict("SHA-256 of the received data does not match", code="checksum_mismatch",
                           details={"expected": row["sha256"], "received": digest})
        files_root = self.storage.files_root(write=True)
        target = contained(files_root, row["path"])
        with _publish_lock:
            if not target.parent.is_dir():
                if not create_parents:
                    self.db.execute("UPDATE uploads SET state='open' WHERE id=?", (upload_id,))
                    raise NotFound("destination directory does not exist", code="parent_missing",
                                   details={"path": str(target.parent.relative_to(files_root))})
                target.parent.mkdir(parents=True, exist_ok=True)
                contained(files_root, str(target.parent.relative_to(files_root)))  # re-check after creation
            if target.exists():
                if row["conflict"] == "fail":
                    self.db.execute("UPDATE uploads SET state='open' WHERE id=?", (upload_id,))
                    raise Conflict("destination exists", code="destination_exists", details={"path": row["path"]})
                target = keep_both_name(target.parent, target.name)
            self.storage.guard.recheck("files", files_root)
            os.rename(partial, target)
        published = str(target.relative_to(files_root))
        self.db.execute("UPDATE uploads SET state='published', published_path=?, sha256=?, updated_at=? WHERE id=?",
                        (published, digest, self.clock.now(), upload_id))
        view = self.view(upload_id)
        view["verification"] = {"size": "matched", "sha256": "matched" if row["sha256"] else "computed_by_server"}
        return view

    def cancel(self, upload_id: str) -> dict:
        row = self._row(upload_id)
        if row["state"] not in ("open", "verifying"):
            raise Conflict(f"upload is {row['state']}", code="upload_not_open")
        self.db.execute("UPDATE uploads SET state='cancelled', updated_at=? WHERE id=?", (self.clock.now(), upload_id))
        try:
            self._partial(upload_id).unlink(missing_ok=True)
        except ServiceError:
            pass  # volume gone: the cleanup job removes the partial later
        return self.view(upload_id)

    def _fail(self, upload_id: str, code: str) -> None:
        self.db.execute("UPDATE uploads SET state='failed', error=?, updated_at=? WHERE id=?", (code, self.clock.now(), upload_id))

    def cleanup_abandoned(self) -> dict:
        """Expire open sessions idle past the abandon window and delete
        partial files that no live session owns."""
        cutoff = self.clock.now() - self.config.limits.staging_abandon_hours * 3600
        uploads_dir = self.storage.staging_area("uploads")
        expired = self.db.all("SELECT id FROM uploads WHERE state IN ('open','verifying') AND updated_at < ?", (cutoff,))
        for r in expired:
            self.db.execute("UPDATE uploads SET state='abandoned', updated_at=? WHERE id=?", (self.clock.now(), r["id"]))
        live = {r["id"] for r in self.db.all("SELECT id FROM uploads WHERE state IN ('open','verifying')")}
        removed = 0
        for p in uploads_dir.glob("*.part"):
            if p.name[: -len(".part")] not in live and not p.is_symlink():
                p.unlink()
                removed += 1
        return {"abandoned_sessions": len(expired), "removed_partials": removed}
