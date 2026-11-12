"""Backup sources, runs, and repository writer coordination.

Exactly one scheduler owns a backup source. A run holds the single
repository writer lease (renewed by heartbeat) in addition to restic's own
locks. A run whose lease lapses becomes `interrupted`; a late report from it
is rejected and can never overwrite a newer successful run.

Protection is evidence from the repository, not from reports: a source is
`current` only when the repository holds a recent snapshot carrying the
source's tag and path, and a run can be finished as `succeeded` only with a
snapshot ID the repository actually contains for that source.
"""
from __future__ import annotations

import re

from ..config import Config
from ..db import Database, dumps, loads
from ..errors import Conflict, NotFound, ServiceError
from ..runtime import Clock, IdFactory, iso
from .restic import Restic, safe_snapshot_id, snapshot_timestamp

REPOSITORY = "primary"
# Status views reuse a snapshot listing for this long; restic is not run per request.
SNAPSHOT_CACHE_SECONDS = 60
_TAG = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# "primary" is the repository Home Services reads; "external:<label>" is a
# repository only the reporting companion can read (e.g. its own key on the
# Mac). External sources are status-only: their runs are reported, not verified.
_REPOSITORY = re.compile(r"^(primary|external:[A-Za-z0-9._/-]{1,100})$")


class BackupCoordinator:
    def __init__(self, config: Config, db: Database, restic: Restic, clock: Clock, ids: IdFactory, *, recovery=None):
        self.config, self.db, self.restic, self.clock, self.ids = config, db, restic, clock, ids
        self.recovery = recovery or restic

    # --- sources ----------------------------------------------------------

    def register_source(self, *, host: str, path: str, tag: str, scheduler: str, principal_id: str | None,
                        repository: str = "primary") -> tuple[dict, bool]:
        if not host or not path.startswith("/"):
            raise ServiceError("host and an absolute source path are required", code="invalid_source")
        if not _TAG.match(tag):
            raise ServiceError("tag must be 1-64 characters of [A-Za-z0-9._-]", code="invalid_tag")
        if not _REPOSITORY.match(repository):
            raise ServiceError("repository must be 'primary' or 'external:<label>'", code="invalid_repository")
        with self.db.tx():
            row = self.db.one("SELECT * FROM backup_sources WHERE host=? AND path=? AND repository=?", (host, path, repository))
            if row is not None:
                if row["scheduler"] != scheduler:
                    raise Conflict("this source is owned by another scheduler", code="source_owned_by_other_scheduler",
                                   details={"source_id": row["id"], "scheduler": row["scheduler"]})
                return self.source(row["id"]), False
            source_id = self.ids.new("src")
            self.db.execute(
                "INSERT INTO backup_sources (id, host, path, tag, scheduler, registered_at, registered_by, repository)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (source_id, host, path, tag, scheduler, self.clock.now(), principal_id, repository),
            )
        return self.source(source_id), True

    def set_scheduler(self, source_id: str, scheduler: str) -> dict:
        """Operator-only ownership transfer (CLI). Refused while a run is live."""
        self.reap()
        if self.db.one("SELECT 1 FROM backup_runs WHERE source_id=? AND state='running'", (source_id,)):
            raise Conflict("a run is active for this source", code="run_active")
        if self.db.execute("UPDATE backup_sources SET scheduler=? WHERE id=?", (scheduler, source_id)) == 0:
            raise NotFound("backup source not found")
        return self.source(source_id)

    def source(self, source_id: str) -> dict:
        row = self.db.one("SELECT * FROM backup_sources WHERE id=?", (source_id,))
        if row is None:
            raise NotFound("backup source not found", details={"source_id": source_id})
        last = self.db.one("SELECT * FROM backup_runs WHERE source_id=? ORDER BY started_at DESC, id DESC LIMIT 1", (source_id,))
        external = row["repository"] != REPOSITORY
        protection = self._reported_protection(source_id) if external else self.protection(row["tag"], row["path"])
        return {"id": row["id"], "host": row["host"], "path": row["path"], "tag": row["tag"], "scheduler": row["scheduler"],
                "repository": row["repository"], "registered_at": iso(row["registered_at"]), "protection": protection,
                "last_run": None if last is None else self._run_view(last)}

    def _reported_protection(self, source_id: str) -> dict:
        """External repositories: the newest run the companion reported as
        succeeded. Not verified by Home Services (it cannot read them)."""
        ok = self.db.one("SELECT * FROM backup_runs WHERE source_id=? AND state='succeeded' ORDER BY finished_at DESC LIMIT 1",
                         (source_id,))
        if ok is None:
            return {"state": "none", "reason": "no successful run has been reported", "evidence": "companion_report", "verified": False}
        age = max(0.0, self.clock.now() - ok["finished_at"])
        summary = loads(ok["summary"]) or {}
        contents = None
        if "total_files" in summary or "total_bytes" in summary:
            contents = {"files": summary.get("total_files"), "bytes": summary.get("total_bytes")}
        return {"state": "overdue" if age > self.overdue_seconds else "current", "last_success_at": iso(ok["finished_at"]),
                "age_seconds": round(age, 3), "snapshot_id": ok["snapshot_id"], "overdue_after_seconds": self.overdue_seconds,
                "evidence": "companion_report", "verified": False, "contents": contents,
                "empty": None if not contents or contents.get("files") is None else contents["files"] == 0}

    @property
    def overdue_seconds(self) -> float:
        return self.config.backup.overdue_hours * 3600

    def _snapshots(self) -> list[dict]:
        return self.restic.snapshots(max_age=SNAPSHOT_CACHE_SECONDS)

    def _state_for(self, snap: dict | None) -> dict:
        if snap is None:
            return {"state": "none", "reason": "the repository holds no snapshot for this source"}
        age = max(0.0, self.clock.now() - snapshot_timestamp(snap["time"]))  # clock skew never makes a snapshot "newer than now"
        contents = snap.get("contents")
        return {"state": "overdue" if age > self.overdue_seconds else "current", "last_success_at": snap["time"],
                "age_seconds": round(age, 3), "snapshot_id": snap["id"], "overdue_after_seconds": self.overdue_seconds,
                "evidence": "repository_snapshot", "contents": contents,
                # An empty snapshot is a successful command that protects nothing.
                "empty": None if not contents or contents.get("files") is None else contents["files"] == 0}

    def protection(self, tag: str, path: str) -> dict:
        """Newest repository snapshot carrying `tag` and covering `path`."""
        try:
            snaps = self._snapshots()
        except ServiceError as exc:
            return {"state": "unknown", "reason": f"the repository could not be read: {exc.message}", "code": exc.code}
        match = next((x for x in snaps if tag in x["tags"] and path in x["paths"]), None)
        return self._state_for(match)

    def groups(self) -> list[dict]:
        """Every (host, paths, tags) series in the repository with its newest
        snapshot, whether or not a source was registered for it."""
        snaps = self.recovery.snapshots(max_age=SNAPSHOT_CACHE_SECONDS)
        sources = self.db.all("SELECT id, tag, path, repository FROM backup_sources")
        seen: dict[tuple, dict] = {}
        for x in snaps:  # newest first
            key = (x.get("repository", "primary"), x["hostname"], tuple(x["paths"]), tuple(sorted(x["tags"])))
            if key in seen:
                seen[key]["snapshot_count"] += 1
                continue
            primary = x.get("repository", "primary") == "primary"
            candidates = [r["id"] for r in sources if r["tag"] in x["tags"] and r["path"] in x["paths"]
                          and (r["repository"] == REPOSITORY) == primary]
            source = candidates[0] if len(candidates) == 1 else None
            seen[key] = {"hostname": x["hostname"], "paths": x["paths"], "tags": x["tags"], "source_id": source,
                         "snapshot_count": 1, "protection": self._state_for(x), "retired": self._retired(x)}
        return list(seen.values())

    def _retired(self, snap: dict) -> dict | None:
        for r in self.config.backup.retired_series:
            if r.host == snap["hostname"] and r.tag in snap["tags"]:
                return {"reason": r.reason or "retired by the operator; snapshots are kept"}
        return None

    def sources(self) -> list[dict]:
        self.reap()
        return [self.source(r["id"]) for r in self.db.all("SELECT id FROM backup_sources ORDER BY registered_at, id")]

    # --- runs -------------------------------------------------------------

    def reap(self) -> None:
        now = self.clock.now()
        with self.db.tx():
            for r in self.db.all("SELECT id FROM backup_runs WHERE state='running' AND lease_expires_at < ?", (now,)):
                self.db.execute("UPDATE backup_runs SET state='interrupted', finished_at=?, error=? WHERE id=?",
                                (now, dumps({"code": "lease_expired", "message": "no heartbeat before the run lease expired"}), r["id"]))
            self.db.execute("DELETE FROM repository_leases WHERE expires_at < ?", (now,))

    def start_run(self, source_id: str, *, scheduler: str, idempotency_key: str | None) -> tuple[dict, bool]:
        src = self.db.one("SELECT * FROM backup_sources WHERE id=?", (source_id,))
        if src is None:
            raise NotFound("backup source not found", details={"source_id": source_id})
        if src["scheduler"] != scheduler:
            raise Conflict("this source is owned by another scheduler", code="source_owned_by_other_scheduler",
                           details={"scheduler": src["scheduler"]})
        repo_key = src["repository"]
        if repo_key == REPOSITORY:
            status = self.restic.repository_status()
            if status["state"] != "present":
                raise ServiceError(f"repository is {status['state']}: {status['reason']}", code="repository_unavailable", status=503,
                                   details={"repository_state": status["state"]})
        self.reap()
        now = self.clock.now()
        ttl = self.config.backup.lease_seconds
        with self.db.tx():
            if idempotency_key:
                row = self.db.one("SELECT * FROM backup_runs WHERE idempotency_key=?", (idempotency_key,))
                if row is not None:
                    if row["source_id"] != source_id:
                        raise Conflict("idempotency key was used for another source", code="idempotency_mismatch")
                    return self._run_view(row), False
            lease = self.db.one("SELECT * FROM repository_leases WHERE repository=?", (repo_key,))
            if lease is not None:
                raise Conflict("another writer holds the repository lease", code="repository_busy",
                               details={"holder_kind": lease["holder_kind"], "expires_at": iso(lease["expires_at"])})
            run_id = self.ids.new("run")
            self.db.execute("INSERT INTO repository_leases (repository, holder_kind, holder_id, acquired_at, expires_at) VALUES (?,?,?,?,?)",
                            (repo_key, "backup_run", run_id, now, now + ttl))
            self.db.execute("INSERT INTO backup_runs (id, source_id, state, idempotency_key, lease_expires_at, started_at, heartbeat_at)"
                            " VALUES (?,?,'running',?,?,?,?)", (run_id, source_id, idempotency_key, now + ttl, now, now))
        return self.run(run_id), True

    def _live_run(self, run_id: str):
        row = self.db.one("SELECT * FROM backup_runs WHERE id=?", (run_id,))
        if row is None:
            raise NotFound("run not found")
        if row["state"] != "running":
            raise Conflict(f"run is {row['state']}; reports for it are no longer accepted", code="run_not_active",
                           details={"state": row["state"]})
        return row

    def heartbeat(self, run_id: str, progress: dict | None) -> dict:
        now = self.clock.now()
        ttl = self.config.backup.lease_seconds
        self.reap()
        with self.db.tx():
            self._live_run(run_id)
            if progress is not None:
                self.db.execute("UPDATE backup_runs SET heartbeat_at=?, lease_expires_at=?, progress=?, progress_at=? WHERE id=?",
                                (now, now + ttl, dumps(progress), now, run_id))
            else:
                self.db.execute("UPDATE backup_runs SET heartbeat_at=?, lease_expires_at=? WHERE id=?", (now, now + ttl, run_id))
            self.db.execute("UPDATE repository_leases SET expires_at=? WHERE holder_id=?", (now + ttl, run_id))
        return self.run(run_id)

    def finish(self, run_id: str, *, state: str, snapshot_id: str | None, summary: dict | None, error: dict | None) -> dict:
        if state not in ("succeeded", "failed", "cancelled"):
            raise ServiceError("state must be succeeded, failed, or cancelled", code="invalid_state")
        verified = None
        if state == "succeeded":
            if not snapshot_id:
                raise ServiceError("a succeeded run must report its snapshot id", code="snapshot_id_required")
            row = self._live_run(run_id)
            src = self.db.one("SELECT tag, path, repository FROM backup_sources WHERE id=?", (row["source_id"],))
        if state == "succeeded" and src["repository"] != REPOSITORY:
            safe_snapshot_id(snapshot_id)  # recorded as reported; Home Services cannot read that repository
        elif state == "succeeded":
            try:
                verified = self.restic.find_snapshot(safe_snapshot_id(snapshot_id))
            except ServiceError as exc:
                raise ServiceError("the repository could not be read to verify the snapshot; retry finish while the run is live",
                                   code="snapshot_unverifiable", status=503, details={"cause": exc.code}) from None
            if verified is None:
                raise Conflict("the repository does not contain that snapshot", code="snapshot_not_found",
                               details={"snapshot_id": snapshot_id})
            if src["tag"] not in verified["tags"] or src["path"] not in verified["paths"]:
                raise Conflict("the snapshot does not carry this source's tag and path", code="snapshot_mismatch",
                               details={"snapshot_id": verified["id"], "expected": {"tag": src["tag"], "path": src["path"]},
                                        "found": {"tags": verified["tags"], "paths": verified["paths"]}})
            snapshot_id = verified["id"]
        now = self.clock.now()
        self.reap()
        with self.db.tx():
            self._live_run(run_id)
            self.db.execute("UPDATE backup_runs SET state=?, snapshot_id=?, summary=?, error=?, finished_at=?, lease_expires_at=NULL WHERE id=?",
                            (state, snapshot_id, dumps(summary), dumps(error), now, run_id))
            self.db.execute("DELETE FROM repository_leases WHERE holder_id=?", (run_id,))
        return self.run(run_id)

    def run(self, run_id: str) -> dict:
        row = self.db.one("SELECT * FROM backup_runs WHERE id=?", (run_id,))
        if row is None:
            raise NotFound("run not found")
        return self._run_view(row)

    def _run_view(self, row) -> dict:
        now = self.clock.now()
        hb, pr = row["heartbeat_at"], row["progress_at"]
        return {
            "id": row["id"], "source_id": row["source_id"], "state": row["state"],
            "started_at": iso(row["started_at"]), "finished_at": iso(row["finished_at"]),
            "lease_expires_at": iso(row["lease_expires_at"]),
            "heartbeat": {"at": iso(hb), "age_seconds": None if hb is None else round(now - hb, 3)},
            "progress": {"at": iso(pr), "age_seconds": None if pr is None else round(now - pr, 3), "data": loads(row["progress"])},
            "snapshot_id": row["snapshot_id"], "summary": loads(row["summary"]), "error": loads(row["error"]),
        }

    def lease(self) -> dict | None:
        self.reap()
        row = self.db.one("SELECT * FROM repository_leases WHERE repository=?", (REPOSITORY,))
        if row is None:
            return None
        return {"holder_kind": row["holder_kind"], "holder_id": row["holder_id"], "acquired_at": iso(row["acquired_at"]),
                "expires_at": iso(row["expires_at"])}
