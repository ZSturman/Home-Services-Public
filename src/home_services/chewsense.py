"""Chew Sense mailbox: recordings wait here while the Mac is away.

The iPhone sends a recording straight to its Mac whenever the Mac answers.
Only when it doesn't does Collect deposit the recording here; the Mac's loop
service collects it when it wakes, verifies and imports it, then acknowledges,
and this copy is deleted. Deposits are encrypted on the phone with a key only
the Mac and its paired phones hold, so this host stores ciphertext it cannot
read. It never imports, processes or inspects recordings.

Blobs live in the guarded `staging` root (the verified HDD), never the state
store. Metadata is a separate SQLite file. A phone token can create, upload,
and read deposits and the status copy; only the Mac token can download,
acknowledge, and publish status.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

from .errors import Conflict, InsufficientSpace, NotFound, ServiceError
from .runtime import Clock, IdFactory, iso
from .storage import Storage

MAX_DEPOSIT = 4 * 1024**3
MAX_HELD = 20 * 1024**3          # open + waiting deposits together
MAX_STATUS = 1_000_000
ABANDON_SECONDS = 7 * 86400      # open deposits idle this long are removed
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SESSION = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
HELD = ("open", "waiting")
RESULTS = {"imported": "collected", "duplicate": "collected", "rejected": "rejected"}


class ChewSenseMailbox:
    def __init__(self, path: Path, storage: Storage, clock: Clock, ids: IdFactory):
        self.storage, self.clock, self.ids = storage, clock, ids
        self.lock = threading.RLock()
        if path.is_symlink():
            raise ServiceError("The Chew Sense mailbox store must be a regular private file")
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS deposits (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
          archive_sha256 TEXT NOT NULL, device TEXT NOT NULL, state TEXT NOT NULL, offset INTEGER NOT NULL,
          receipt TEXT, created_by TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS deposit_state ON deposits(state, created_at);
        CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
        """)

    # --- helpers ---------------------------------------------------------------

    def _area(self) -> Path:
        return self.storage.staging_area("chewsense")

    def _blob(self, deposit_id: str) -> Path:
        return self._area() / f"{deposit_id}.sealed"

    def _row(self, deposit_id: str):
        row = self.db.execute("SELECT * FROM deposits WHERE id=?", (deposit_id,)).fetchone()
        if row is None:
            raise NotFound("deposit not found", details={"deposit_id": deposit_id})
        return row

    def view(self, deposit_id: str) -> dict:
        r = self._row(deposit_id)
        return {"id": r["id"], "session_id": r["session_id"], "sha256": r["sha256"], "size": r["size"],
                "archive_sha256": r["archive_sha256"], "device": r["device"], "state": r["state"],
                "offset": r["offset"], "receipt": json.loads(r["receipt"]) if r["receipt"] else None,
                "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"])}

    def _set(self, deposit_id: str, **fields) -> None:
        columns = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE deposits SET {columns}, updated_at=? WHERE id=?",
                        (*fields.values(), self.clock.now(), deposit_id))
        self.db.commit()

    # --- phone -----------------------------------------------------------------

    def create(self, body: dict, principal_id: str | None) -> tuple[dict, bool]:
        session_id, sha, archive = body.get("session_id"), body.get("sha256"), body.get("archive_sha256")
        size, device = body.get("size"), body.get("device", "iPhone")
        if not (isinstance(session_id, str) and _SESSION.match(session_id)):
            raise ServiceError("session_id is required", code="invalid_session_id")
        if not (isinstance(sha, str) and _SHA256.match(sha) and isinstance(archive, str) and _SHA256.match(archive)):
            raise ServiceError("sha256 and archive_sha256 must be 64 lowercase hex characters", code="invalid_sha256")
        if not isinstance(size, int) or not 0 < size <= MAX_DEPOSIT:
            raise ServiceError("size must be between 1 byte and 4 GB", code="invalid_size", status=413)
        if not isinstance(device, str) or len(device) > 80:
            raise ServiceError("device must be a short name", code="invalid_device")
        with self.lock:
            self.cleanup_abandoned()
            existing = self.db.execute("SELECT id, session_id, size FROM deposits WHERE sha256=?", (sha,)).fetchone()
            if existing is not None:
                if (existing["session_id"], existing["size"]) != (session_id, size):
                    raise Conflict("this sealed archive was deposited for another recording", code="deposit_mismatch")
                return self.view(existing["id"]), False
            held = self.db.execute("SELECT COALESCE(SUM(size),0) FROM deposits WHERE state IN (?,?)", HELD).fetchone()[0]
            if held + size > MAX_HELD:
                raise InsufficientSpace("the mailbox is full; recordings stay on the iPhone until the Mac collects some",
                                        details={"held_bytes": held, "limit_bytes": MAX_HELD})
            self.storage.ensure_space(size)
            deposit_id = self.ids.new("dep")
            self._blob(deposit_id).touch(exist_ok=False)
            now = self.clock.now()
            self.db.execute("INSERT INTO deposits VALUES (?,?,?,?,?,?,'open',0,NULL,?,?,?)",
                            (deposit_id, session_id, sha, size, archive, device, principal_id, now, now))
            self.db.commit()
            return self.view(deposit_id), True

    def append(self, deposit_id: str, offset: int, chunks, max_chunk: int) -> dict:
        with self.lock:
            row = self._row(deposit_id)
            if row["state"] != "open":
                raise Conflict(f"deposit is {row['state']}", code="deposit_not_open")
            blob = self._blob(deposit_id)
            if not blob.exists():
                self._set(deposit_id, state="abandoned")
                raise Conflict("the partial deposit is missing; send it again", code="partial_missing")
            acknowledged = row["offset"]
            if blob.stat().st_size != acknowledged:  # bytes past the commit after a crash
                with open(blob, "r+b") as f:
                    f.truncate(acknowledged)
            if offset != acknowledged:
                raise Conflict("offset does not match the server's acknowledged offset", code="offset_mismatch",
                               details={"expected_offset": acknowledged})
            root = self.storage.guard.root_path("staging", write=True)
            written = 0
            with open(blob, "r+b") as f:
                f.seek(offset)
                for chunk in chunks:
                    written += len(chunk)
                    if written > max_chunk or offset + written > row["size"]:
                        f.truncate(offset)
                        raise ServiceError("chunk exceeds the chunk limit or the declared size",
                                           code="chunk_too_large", status=413)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            self.storage.guard.recheck("staging", root)
            self._set(deposit_id, offset=offset + written)
            return self.view(deposit_id)

    def complete(self, deposit_id: str) -> dict:
        with self.lock:
            row = self._row(deposit_id)
            if row["state"] != "open":
                return self.view(deposit_id)  # idempotent replay
            if row["offset"] != row["size"]:
                raise Conflict("deposit is incomplete", code="deposit_incomplete",
                               details={"offset": row["offset"], "size": row["size"]})
            digest = hashlib.sha256()
            with open(self._blob(deposit_id), "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    digest.update(block)
            if digest.hexdigest() != row["sha256"]:
                with open(self._blob(deposit_id), "r+b") as f:
                    f.truncate(0)
                self._set(deposit_id, offset=0)
                raise Conflict("SHA-256 of the received data does not match; send it again from the start",
                               code="checksum_mismatch")
            self._set(deposit_id, state="waiting")
            return self.view(deposit_id)

    def cancel(self, deposit_id: str) -> dict:
        """The phone reached the Mac directly; an unfinished deposit is no longer needed."""
        with self.lock:
            row = self._row(deposit_id)
            if row["state"] not in HELD:
                return self.view(deposit_id)
            self._blob(deposit_id).unlink(missing_ok=True)
            self._set(deposit_id, state="cancelled", offset=0)
            return self.view(deposit_id)

    # --- Mac -------------------------------------------------------------------

    def pending(self) -> list[dict]:
        rows = self.db.execute("SELECT id FROM deposits WHERE state='waiting' ORDER BY created_at LIMIT 50").fetchall()
        return [self.view(r["id"]) for r in rows]

    def blob(self, deposit_id: str) -> Path:
        if self._row(deposit_id)["state"] != "waiting":
            raise Conflict("only waiting deposits can be downloaded", code="deposit_not_waiting")
        return self._blob(deposit_id)

    def acknowledge(self, deposit_id: str, body: dict) -> dict:
        result = body.get("result")
        if result not in RESULTS:
            raise ServiceError("result must be imported, duplicate or rejected", code="invalid_result")
        detail = body.get("detail")
        receipt = {"result": result, "session_id": body.get("session_id"),
                   "detail": str(detail)[:500] if detail else None, "acknowledged_at": iso(self.clock.now())}
        with self.lock:
            row = self._row(deposit_id)
            if row["state"] in RESULTS.values():
                return self.view(deposit_id)  # idempotent replay
            if row["state"] != "waiting":
                raise Conflict(f"deposit is {row['state']}", code="deposit_not_waiting")
            self._set(deposit_id, state=RESULTS[result], receipt=json.dumps(receipt, sort_keys=True))
            self._blob(deposit_id).unlink(missing_ok=True)
            return self.view(deposit_id)

    def put_status(self, value) -> dict:
        record = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(record.encode()) > MAX_STATUS:
            raise ServiceError("the status copy exceeds 1 MB", status=413)
        now = self.clock.now()
        with self.lock:
            self.db.execute("INSERT INTO status VALUES ('status',?,?) ON CONFLICT(key) DO UPDATE "
                            "SET value=excluded.value, updated_at=excluded.updated_at", (record, now))
            self.db.commit()
        return {"updated_at": iso(now)}

    def get_status(self) -> dict:
        row = self.db.execute("SELECT value, updated_at FROM status WHERE key='status'").fetchone()
        if row is None:
            raise NotFound("the Mac has not published a status copy yet")
        return {"status": json.loads(row["value"]), "updated_at": iso(row["updated_at"])}

    # --- housekeeping ----------------------------------------------------------

    def cleanup_abandoned(self) -> int:
        cutoff = self.clock.now() - ABANDON_SECONDS
        with self.lock:
            rows = self.db.execute("SELECT id FROM deposits WHERE state='open' AND updated_at < ?", (cutoff,)).fetchall()
            for r in rows:
                self._blob(r["id"]).unlink(missing_ok=True)
                self._set(r["id"], state="abandoned", offset=0)
            return len(rows)

    def summary(self) -> dict:
        counts = dict(self.db.execute("SELECT state, COUNT(*) FROM deposits GROUP BY state").fetchall())
        held = self.db.execute("SELECT COALESCE(SUM(size),0) FROM deposits WHERE state IN (?,?)", HELD).fetchone()[0]
        return {"deposits": counts, "held_bytes": held, "limit_bytes": MAX_HELD}
