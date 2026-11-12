"""Durable jobs.

Jobs live in SQLite and run in supervised worker processes, not inside the
gateway. A worker claims a job with a lease and renews it with heartbeats.
Heartbeat (the worker is alive) and progress (the work actually advanced)
are recorded separately. A running job whose lease expires is marked
`interrupted`; it is never silently resumed or reported as succeeded.
"""
from __future__ import annotations

import hashlib
import json

from .db import Database, dumps, loads
from .errors import Conflict, NotFound, ServiceError
from .runtime import Clock, IdFactory, iso

STATES = ("waiting", "running", "blocked", "succeeded", "failed", "cancelled", "interrupted")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")

# kind -> (queue, client-creatable)
KINDS = {
    "storage.cleanup": ("storage", True),
    "backup.restore": ("backup", False),  # created via POST /backups/restores
}


def request_hash(kind: str, params: dict) -> str:
    return hashlib.sha256(json.dumps([kind, params], sort_keys=True).encode()).hexdigest()


class JobStore:
    def __init__(self, db: Database, clock: Clock, ids: IdFactory):
        self.db, self.clock, self.ids = db, clock, ids

    def create(self, kind: str, params: dict, *, idempotency_key: str | None, source: str | None = None, created_by: str | None = None) -> tuple[dict, bool]:
        """Returns (job, created). Replaying an idempotency key with the same
        request returns the original job; a different request is a conflict."""
        if kind not in KINDS:
            raise ServiceError(f"unknown job kind {kind!r}", code="unknown_job_kind")
        queue = KINDS[kind][0]
        rhash = request_hash(kind, params)
        with self.db.tx():
            if idempotency_key:
                row = self.db.one("SELECT * FROM jobs WHERE kind=? AND idempotency_key=?", (kind, idempotency_key))
                if row is not None:
                    if row["request_hash"] != rhash:
                        raise Conflict("idempotency key was already used for a different request", code="idempotency_mismatch",
                                       details={"job_id": row["id"]})
                    return self._view(row), False
            job_id = self.ids.new("job")
            self.db.execute(
                "INSERT INTO jobs (id, kind, queue, source, state, idempotency_key, request_hash, params, created_at, created_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, kind, queue, source, "waiting", idempotency_key, rhash, dumps(params), self.clock.now(), created_by),
            )
        return self.get(job_id), True

    def get(self, job_id: str) -> dict:
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            raise NotFound("job not found", details={"job_id": job_id})
        return self._view(row)

    def list(self, *, state: str | None = None, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if state:
            sql += " AND state=?"; args.append(state)
        if kind:
            sql += " AND kind=?"; args.append(kind)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"; args.append(max(1, min(limit, 200)))
        return [self._view(r) for r in self.db.all(sql, tuple(args))]

    def request_cancel(self, job_id: str) -> dict:
        with self.db.tx():
            job = self.get(job_id)
            if job["state"] in TERMINAL:
                raise Conflict(f"job is already {job['state']}", code="job_finished")
            if job["state"] in ("waiting", "blocked"):
                self.db.execute("UPDATE jobs SET state='cancelled', cancel_requested=1, finished_at=? WHERE id=?", (self.clock.now(), job_id))
            else:
                self.db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
        return self.get(job_id)

    # --- worker side -----------------------------------------------------

    def reap_expired(self) -> list[str]:
        """Mark running jobs whose lease expired as interrupted."""
        now = self.clock.now()
        with self.db.tx():
            rows = self.db.all("SELECT id FROM jobs WHERE state='running' AND lease_expires_at < ?", (now,))
            for r in rows:
                self.db.execute(
                    "UPDATE jobs SET state='interrupted', finished_at=?, lease_owner=NULL, error=? WHERE id=?",
                    (now, dumps({"code": "lease_expired", "message": "the worker stopped renewing its lease before the job finished"}), r["id"]),
                )
        return [r["id"] for r in rows]

    def claim(self, queue: str, owner: str, lease_seconds: int) -> dict | None:
        now = self.clock.now()
        with self.db.tx():
            row = self.db.one(
                "SELECT * FROM jobs WHERE queue=? AND cancel_requested=0 AND"
                " (state='waiting' OR (state='blocked' AND (retry_at IS NULL OR retry_at <= ?))) ORDER BY created_at, id LIMIT 1",
                (queue, now),
            )
            if row is None:
                return None
            self.db.execute(
                "UPDATE jobs SET state='running', lease_owner=?, lease_expires_at=?, heartbeat_at=?, started_at=COALESCE(started_at, ?),"
                " attempts=attempts+1, error=NULL WHERE id=?",
                (owner, now + lease_seconds, now, now, row["id"]),
            )
        return self.get(row["id"])

    def heartbeat(self, job_id: str, owner: str, lease_seconds: int, progress: dict | None = None) -> bool:
        """Renew the lease; returns True if cancellation was requested. Raises
        if this worker no longer owns the job (it must stop immediately)."""
        now = self.clock.now()
        with self.db.tx():
            row = self.db.one("SELECT lease_owner, state, cancel_requested FROM jobs WHERE id=?", (job_id,))
            if row is None or row["lease_owner"] != owner or row["state"] != "running":
                raise Conflict("job lease lost", code="lease_lost")
            if progress is not None:
                self.db.execute("UPDATE jobs SET lease_expires_at=?, heartbeat_at=?, progress=?, progress_at=? WHERE id=?",
                                (now + lease_seconds, now, dumps(progress), now, job_id))
            else:
                self.db.execute("UPDATE jobs SET lease_expires_at=?, heartbeat_at=? WHERE id=?", (now + lease_seconds, now, job_id))
            return bool(row["cancel_requested"])

    def finish(self, job_id: str, owner: str, state: str, *, result: dict | None = None, error: dict | None = None,
               retry_after: float = 60) -> None:
        """Terminal states end the job; `blocked` (e.g. the volume is missing)
        parks it until `retry_after` seconds have passed."""
        if state not in ("succeeded", "failed", "cancelled", "interrupted", "blocked"):
            raise ValueError(state)
        now = self.clock.now()
        with self.db.tx():
            row = self.db.one("SELECT lease_owner, state FROM jobs WHERE id=?", (job_id,))
            if row is None or row["lease_owner"] != owner or row["state"] != "running":
                raise Conflict("job lease lost", code="lease_lost")
            finished = None if state == "blocked" else now
            retry_at = now + retry_after if state == "blocked" else None
            self.db.execute(
                "UPDATE jobs SET state=?, result=?, error=?, finished_at=?, retry_at=?, lease_owner=NULL, lease_expires_at=NULL WHERE id=?",
                (state, dumps(result), dumps(error), finished, retry_at, job_id),
            )

    def _view(self, row) -> dict:
        now = self.clock.now()
        hb, pr = row["heartbeat_at"], row["progress_at"]
        return {
            "id": row["id"], "kind": row["kind"], "source": row["source"], "state": row["state"],
            "params": loads(row["params"]), "result": loads(row["result"]), "error": loads(row["error"]),
            "cancel_requested": bool(row["cancel_requested"]), "attempts": row["attempts"],
            "created_at": iso(row["created_at"]), "started_at": iso(row["started_at"]), "finished_at": iso(row["finished_at"]),
            "heartbeat": {"at": iso(hb), "age_seconds": None if hb is None else round(now - hb, 3)},
            "progress": {"at": iso(pr), "age_seconds": None if pr is None else round(now - pr, 3), "data": loads(row["progress"])},
        }
