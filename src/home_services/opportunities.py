"""Durable opportunity review relay. The Mac remains the only Opportunity Discovery writer.

The Mac companion publishes read-only JSON copies of the phone API's GET responses
("resources", keyed by that path) and applies queued phone requests through its own
revision-checked routes. The relay never runs workflow logic, verifies recipients,
or sends email: outbox dispatch, test and delivery routes are not accepted.
Separate SQLite state keeps this independent from storage/camera workers and articles.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

from .auth import Principal
from .errors import Conflict, NotFound, ServiceError
from .runtime import Clock, iso

MAX_BODY = 25_000_000
MAX_RESOURCE = 2_000_000
MAX_RESOURCES = 128_000_000
MAX_KEYS = 5000
MAX_REQUEST = 1_000_000  # the Mac's phone API accepts the same size
MAX_RESULT = 2_000_000
IDENT = r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}"
# Exactly the mutations the iPhone app makes. Sending, dispatch, test receipts,
# manual-contact records and lifecycle stage changes stay on the Mac.
SUBMIT_PATHS = [
    re.compile(rf"/api/v2/review/(?P<case>{IDENT})/(?:decision|offer|notes|contact|proof|draft|compose|approve-draft)"),
    re.compile(r"/api/v2/outbox/schedule"),
    re.compile(rf"/api/v2/outbox/{IDENT}/(?:cancel|resolve)"),
]
KEY = re.compile(r"/api/[A-Za-z0-9_./:?=&%-]{1,290}")
SYNC_ACTIONS = {"register", "sync", "pending", "resolve"}
READ_ACTIONS = {"get", "operations"}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def text(body, key):
    value = body.get(key)
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ServiceError(f"A bounded {key} string is required")
    return value


def submitted_case(path, body):
    """The review case an operation changes, so later phone steps can chain onto it."""
    for pattern in SUBMIT_PATHS:
        match = pattern.fullmatch(path)
        if match:
            if "case" in match.groupdict():
                return match["case"]
            case = body.get("case_id")
            return case if isinstance(case, str) and re.fullmatch(IDENT, case) else None
    raise ServiceError("This request cannot be queued; sending and dispatch stay on the Mac")


class OpportunityRelay:
    def __init__(self, path: Path, clock: Clock, server_id: str):
        self.clock, self.server_id = clock, server_id
        self.lock = threading.RLock()
        if path.is_symlink():
            raise ServiceError("The opportunity state store must be a regular private file")
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS resources (key TEXT PRIMARY KEY, digest TEXT NOT NULL, value TEXT NOT NULL, bytes INTEGER NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS operations (
          id TEXT PRIMARY KEY, path TEXT NOT NULL, case_id TEXT, after TEXT, request TEXT NOT NULL, request_hash TEXT NOT NULL,
          request_bytes INTEGER NOT NULL, state TEXT NOT NULL, result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS operation_state ON operations(state, created_at);
        """)

    def state(self, key):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key, value):
        self.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def touch(self):
        if self.clock.now() - float(self.state("companion_seen_epoch") or 0) >= 30:
            self.set_state("companion_seen_at", iso(self.clock.now()))
            self.set_state("companion_seen_epoch", self.clock.now())

    def resource(self, key):
        row = self.db.execute("SELECT value, updated_at FROM resources WHERE key=?", (key,)).fetchone()
        return (json.loads(row[0]), iso(row[1])) if row else (None, None)

    def operation(self, request_id):
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise NotFound("Operation not found")
        return {"request_id": row["id"], "path": row["path"], "case_id": row["case_id"], "after": row["after"],
                "state": row["state"], "result": json.loads(row["result"]) if row["result"] else None,
                "created_at": iso(row["created_at"]), "updated_at": iso(row["updated_at"])}

    def put_resources(self, resources):
        if not isinstance(resources, dict) or len(resources) > 500:
            raise ServiceError("resources must map up to 500 keys to JSON values")
        for key, value in resources.items():
            if not isinstance(key, str) or not KEY.fullmatch(key):
                raise ServiceError("Resource keys are phone API paths")
            record = encoded(value)
            size = len(record.encode())
            if size > MAX_RESOURCE:
                raise ServiceError("A resource exceeds 2 MB", status=413)
            used, count = self.db.execute("SELECT COALESCE(SUM(bytes),0), COUNT(*) FROM resources WHERE key<>?", (key,)).fetchone()
            if used + size > MAX_RESOURCES or count >= MAX_KEYS:
                raise ServiceError("Opportunity resource quota reached (128 MB / 5,000 keys)", status=507)
            self.db.execute("INSERT INTO resources VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET digest=excluded.digest,"
                            "value=excluded.value,bytes=excluded.bytes,updated_at=excluded.updated_at",
                            (key, hashlib.sha256(record.encode()).hexdigest(), record, size, self.clock.now()))

    def handle(self, body: dict, principal: Principal):
        if not isinstance(body, dict):
            raise ServiceError("Relay request must be an object")
        action = body.get("action")
        if not isinstance(action, str):
            raise ServiceError("A relay action string is required")
        scope = ("opportunities:sync" if action in SYNC_ACTIONS else "opportunities:review" if action == "submit"
                 else "opportunities:read" if action in READ_ACTIONS else None)
        if scope is None:
            raise ServiceError("Unsupported opportunity action; the relay never sends or dispatches email")
        principal.require(scope)
        with self.lock, self.db:
            workspace = self.state("workspace_id")
            if action == "register" and workspace is None:
                workspace = text(body, "workspace_id")
                self.set_state("workspace_id", workspace)
            if action not in READ_ACTIONS and (not workspace or body.get("workspace_id") != workspace):
                raise Conflict("Workspace identity differs; reconnect to the correct Opportunity Discovery database", code="workspace_mismatch")
            identity = {"server_id": self.server_id, "workspace_id": workspace,
                        "companion_seen_at": self.state("companion_seen_at")}
            if action == "register":
                self.touch()
                return {"status": "ok", "digests": {r[0]: r[1] for r in self.db.execute("SELECT key, digest FROM resources")}, **identity}
            if action == "get":
                keys = body.get("keys", [body.get("key")])
                if not isinstance(keys, list) or not 0 < len(keys) <= 100 or not all(isinstance(k, str) and KEY.fullmatch(k) for k in keys):
                    raise ServiceError("key or keys (up to 100) must be phone API paths")
                found = {}
                for key in keys:
                    value, updated = self.resource(key)
                    if value is not None:
                        found[key] = {"value": value, "updated_at": updated}
                return {"status": "ok", "resources": found, **identity}
            if action == "operations":
                ids = body.get("request_ids")
                if not isinstance(ids, list) or len(ids) > 500 or not all(isinstance(i, str) for i in ids):
                    raise ServiceError("request_ids must be a bounded list")
                rows = [self.db.execute("SELECT id FROM operations WHERE id=?", (i,)).fetchone() for i in ids]
                return {"status": "ok", "operations": [self.operation(r[0]) for r in rows if r], **identity}
            if action == "sync":
                self.put_resources(body.get("resources", {}))
                remove = body.get("remove", [])
                if not isinstance(remove, list) or len(remove) > MAX_KEYS or not all(isinstance(k, str) for k in remove):
                    raise ServiceError("remove must be a bounded list of keys")
                for key in remove:
                    self.db.execute("DELETE FROM resources WHERE key=?", (key,))
                self.touch()
                return {"status": "ok", **identity}
            if action == "pending":
                rows = self.db.execute("SELECT id, path, after, request FROM operations WHERE state='queued' ORDER BY created_at, rowid LIMIT 25").fetchall()
                self.touch()
                return {"status": "ok", "operations": [{"request_id": r[0], "path": r[1], "after": r[2], "request": json.loads(r[3])} for r in rows], **identity}
            if action == "resolve":
                request_id = text(body, "request_id")
                operation = self.operation(request_id)
                state, result = body.get("state"), body.get("result")
                if state not in {"applied", "conflict"} or not isinstance(result, dict):
                    raise ServiceError("A final applied/conflict result is required")
                if operation["state"] != "queued":
                    return {"status": "ok", "operation": operation, **identity}
                if state == "applied" and result.get("status") != "ok":
                    raise ServiceError("Applied operations require a successful Mac result")
                if state == "conflict" and not isinstance(result.get("reason"), str):
                    raise ServiceError("Conflicts need a human-readable reason")
                receipt = encoded(result)
                if len(receipt.encode()) > MAX_RESULT:
                    raise ServiceError("Operation result exceeds 2 MB", status=413)
                # Refreshed copies land with the receipt, so the phone never sees an applied
                # receipt next to a case that still shows the old revision.
                self.put_resources(body.get("resources", {}))
                self.db.execute("UPDATE operations SET state=?, result=?, updated_at=? WHERE id=?",
                                (state, receipt, self.clock.now(), request_id))
                return {"status": "ok", "operation": self.operation(request_id), **identity}
            # submit
            if set(body) - {"action", "workspace_id", "request_id", "path", "body", "after"}:
                raise ServiceError("Unsupported submit fields")
            request_id, path = text(body, "request_id"), text(body, "path")
            request = body.get("body")
            if not isinstance(request, dict):
                raise ServiceError("body must be the JSON object the Mac route expects")
            case_id = submitted_case(path, request)
            after = body.get("after")
            if after is not None:
                after = text(body, "after")
            stored = {"path": path, "body": request, "after": after}
            payload = encoded(stored)
            if len(payload.encode()) > MAX_REQUEST:
                raise ServiceError("Request exceeds 1 MB", status=413)
            fingerprint = hashlib.sha256(payload.encode()).hexdigest()
            old = self.db.execute("SELECT request_hash FROM operations WHERE id=?", (request_id,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise Conflict("A request ID cannot be reused for a different change")
                return {"status": "queued", "operation": self.operation(request_id), **identity}
            count, used = self.db.execute("SELECT COUNT(*), COALESCE(SUM(request_bytes),0) FROM operations").fetchone()
            if count >= 10000 or used + len(payload.encode()) > 64_000_000:
                raise ServiceError("Operation receipt quota reached (10,000 / 64 MB); export and rotate the relay", status=507)
            state, result = "queued", None
            if after is not None:
                if self.db.execute("SELECT 1 FROM operations WHERE id=?", (after,)).fetchone() is None:
                    raise ServiceError("after must name an earlier operation from this phone")
            elif case_id and (path.startswith("/api/v2/review/") or path == "/api/v2/outbox/schedule"):
                current, _ = self.resource(f"/api/v2/review/{case_id}")
                if current is not None and current.get("revision") != request.get("revision"):
                    state = "conflict"
                    result = encoded({"status": "refused", "reason": "This case changed on the Mac. Your change is preserved; reload and apply it again."})
            now = self.clock.now()
            self.db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (request_id, path, case_id, after, payload, fingerprint, len(payload.encode()), state, result, now, now))
            return {"status": "queued", "operation": self.operation(request_id), **identity}
