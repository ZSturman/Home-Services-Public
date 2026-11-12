"""Durable article review relay. The Mac remains the only editorial writer.

Separate SQLite state avoids coupling article queues to storage/camera workers.
No publication/generation actions, paths, subprocesses, or model credentials here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

from .auth import Principal
from .errors import Conflict, NotFound, ServiceError
from .runtime import Clock, iso

MAX_BODY = 30_000_000
MAX_SNAPSHOTS = 512_000_000
MAX_EDIT = 2_000_000
MAX_MEDIA_EDIT = 16_000_000  # base64 phone images; the Mac validates names, hashes and formats
MAX_PENDING_BATCH = 24_000_000
MUTATIONS = {
    "save_article": {"markdown", "title", "summary", "tags", "cover_image", "media"},
    "approve": {"review_digest"}, "archive": set(), "restore": set(),
}
READ_FIELDS = {"id", "title", "slug", "summary", "tags", "projects", "cover", "markdown", "workspace",
               "revision", "review_digest", "approval", "status", "asset_error", "post_on", "published_at",
               "platforms", "platform_urls", "declined", "mobile_assets", "mobile_asset_error"}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def text(body, key):
    value = body.get(key)
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ServiceError(f"A bounded {key} string is required")
    return value


class ArticleRelay:
    def __init__(self, path: Path, clock: Clock, server_id: str):
        self.clock, self.server_id = clock, server_id
        self.lock = threading.RLock()
        if path.is_symlink():
            raise ServiceError("The article state store must be a regular private file")
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, revision TEXT NOT NULL, record TEXT NOT NULL, bytes INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews (
          id TEXT PRIMARY KEY, article_id TEXT NOT NULL, request TEXT NOT NULL, request_hash TEXT NOT NULL, request_bytes INTEGER NOT NULL,
          state TEXT NOT NULL, result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS review_state ON reviews(state, created_at);
        """)

    def state(self, key):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key, value):
        self.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def touch(self):
        # Durable reviews need FULL sync; idle heartbeat reads do not need three writes per poll.
        if self.clock.now() - float(self.state("companion_seen_epoch") or 0) >= 30:
            self.set_state("companion_seen_at", iso(self.clock.now()))
            self.set_state("companion_seen_epoch", self.clock.now())

    def snapshot(self, article_id):
        row = self.db.execute("SELECT record FROM snapshots WHERE id=?", (article_id,)).fetchone()
        if row is None:
            raise NotFound("Article is not available in the relay; wait for the Mac companion")
        return json.loads(row[0])

    def operation(self, request_id):
        row = self.db.execute("SELECT * FROM reviews WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise NotFound("Review operation not found")
        return {"request_id": row["id"], "article_id": row["article_id"], "state": row["state"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "updated_at": iso(row["updated_at"])}

    def put_snapshot(self, article):
        if not isinstance(article, dict):
            raise ServiceError("article must be an object")
        article_id, revision = text(article, "id"), text(article, "revision")
        if set(article) - READ_FIELDS:
            raise ServiceError("Snapshot contains unsupported or private workspace fields")
        if not isinstance(article.get("markdown"), str) or not isinstance(article.get("workspace"), dict):
            raise ServiceError("Snapshot needs Markdown and editorial completion state")
        record = encoded(article)
        size = len(record.encode())
        if size > MAX_BODY:
            raise ServiceError("Article snapshot exceeds the 30 MB limit", status=413)
        used = self.db.execute("SELECT COALESCE(SUM(bytes),0) FROM snapshots WHERE id<>?", (article_id,)).fetchone()[0]
        if used + size > MAX_SNAPSHOTS:
            raise ServiceError("Article snapshot quota reached (512 MB); reduce cached media", status=507)
        self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,record=excluded.record,bytes=excluded.bytes",
                        (article_id, revision, record, size))

    def handle(self, body: dict, principal: Principal):
        if not isinstance(body, dict):
            raise ServiceError("Review request must be an object")
        action = body.get("action")
        if not isinstance(action, str):
            raise ServiceError("A review action string is required")
        scope = "articles:sync" if action in {"register", "sync_snapshot", "reconcile", "pending", "resolve"} else "articles:review" if action in MUTATIONS else "articles:read"
        principal.require(scope)
        with self.lock, self.db:
            workspace = self.state("workspace_id")
            if action in {"register", "sync_snapshot"} and workspace is None:
                workspace = text(body, "workspace_id")
                self.set_state("workspace_id", workspace)
            if action not in {"list", "get", "operation"}:
                if not workspace or body.get("workspace_id") != workspace:
                    raise Conflict("Workspace identity differs; reconnect to the correct article workspace", code="workspace_mismatch")
            identity = {"server_id": self.server_id, "workspace_id": workspace,
                        "companion_seen_at": self.state("companion_seen_at")}
            if action == "register":
                self.touch()
                return {"status": "ok", "revisions": {r[0]: r[1] for r in self.db.execute("SELECT id,revision FROM snapshots")}, **identity}
            if action == "list":
                records = [json.loads(row[0]) for row in self.db.execute("SELECT record FROM snapshots ORDER BY id")]
                for record in records:
                    record.pop("mobile_assets", None)
                    record["markdown"] = record["markdown"][:2048]
                    record["body_complete"] = False
                return {"status": "ok", "articles": records, **identity}
            if action == "get":
                return {"status": "ok", "article": self.snapshot(text(body, "article_id")), **identity}
            if action == "operation":
                return {"status": "ok", "operation": self.operation(text(body, "request_id")), **identity}
            if action == "sync_snapshot":
                self.put_snapshot(body.get("article"))
                self.touch()
                return {"status": "ok", **identity}
            if action == "reconcile":
                ids = body.get("article_ids")
                if not isinstance(ids, list) or len(ids) > 5000 or not all(isinstance(i, str) for i in ids):
                    raise ServiceError("article_ids must be a bounded list")
                known = [row[0] for row in self.db.execute("SELECT id FROM snapshots")]
                for article_id in set(known) - set(ids):
                    self.db.execute("DELETE FROM snapshots WHERE id=?", (article_id,))
                self.touch()
                return {"status": "ok", **identity}
            if action == "pending":
                rows = self.db.execute("SELECT id, request, request_bytes FROM reviews WHERE state='queued' ORDER BY created_at,id LIMIT 20").fetchall()
                batch, size = [], 0
                for row in rows:
                    # Bound the Mac's response; image-bearing edits wait for the next poll.
                    if batch and size + row[2] > MAX_PENDING_BATCH:
                        break
                    batch.append(row); size += row[2]
                self.touch()
                return {"status": "ok", "operations": [{"request_id": r[0], "request": json.loads(r[1])} for r in batch], **identity}
            if action == "resolve":
                request_id = text(body, "request_id")
                operation = self.operation(request_id)
                state, result = body.get("state"), body.get("result")
                if state not in {"applied", "conflict"} or not isinstance(result, dict):
                    raise ServiceError("A final applied/conflict result is required")
                if operation["state"] != "queued":
                    return {"status": "ok", "operation": operation, **identity}
                if state == "applied":
                    if result.get("status") not in {"ok", "completed"}:
                        raise ServiceError("Applied operations require a successful backend result")
                    article = result.get("article")
                    if article is not None:
                        self.put_snapshot(article)
                        result = {**result, "article_id": article["id"], "revision": article["revision"]}
                    else:
                        text(result, "article_id")
                        text(result, "revision")
                    if result["article_id"] != operation["article_id"]:
                        self.db.execute("DELETE FROM snapshots WHERE id=?", (operation["article_id"],))
                # Full snapshots are in snapshots; receipts stay small and survive cache replacement.
                receipt = {k: v for k, v in result.items() if k != "article"}
                if state == "applied":
                    receipt["article_id"] = result["article_id"]
                    receipt["revision"] = result["revision"]
                self.db.execute("UPDATE reviews SET state=?,result=?,updated_at=? WHERE id=?",
                                (state, encoded(receipt), self.clock.now(), request_id))
                # Image bytes are no longer needed once resolved; request_hash still detects a reused ID.
                stored = json.loads(self.db.execute("SELECT request FROM reviews WHERE id=?", (request_id,)).fetchone()[0])
                if isinstance(stored.get("media"), dict):
                    stored["media"] = sorted(stored["media"])
                    slim = encoded(stored)
                    self.db.execute("UPDATE reviews SET request=?,request_bytes=? WHERE id=?", (slim, len(slim.encode()), request_id))
                return {"status": "ok", "operation": self.operation(request_id), **identity}
            if action in MUTATIONS:
                allowed = {"action", "article_id", "revision", "request_id", "workspace_id"} | MUTATIONS[action]
                if set(body) - allowed:
                    raise ServiceError("Unsupported review fields")
                request_id, article_id = text(body, "request_id"), text(body, "article_id")
                revision = text(body, "revision")
                request = {k: v for k, v in body.items() if k not in {"workspace_id", "request_id"}}
                for key in MUTATIONS[action] - {"tags", "media"}:
                    if key in request and not isinstance(request[key], str):
                        raise ServiceError("Review fields must be strings")
                cover = request.get("cover_image")
                if cover and not cover.startswith("https://") and (cover.startswith("/") or "\\" in cover or len(cover) > 300
                                                                   or any(part in ("", "..") or part.startswith(".") for part in cover.split("/"))):
                    raise ServiceError("cover_image must be an image path inside the article folder")
                media = request.get("media")
                if media is not None and (not isinstance(media, dict) or not 0 < len(media) <= 12
                                          or not all(isinstance(k, str) and len(k) <= 200 and isinstance(v, str) for k, v in media.items())):
                    raise ServiceError("media must map up to 12 image names to base64 strings")
                if "tags" in request and (not isinstance(request["tags"], list) or not all(isinstance(v, str) for v in request["tags"])):
                    raise ServiceError("tags must be strings")
                if action == "approve":
                    text(request, "review_digest")
                if action == "save_article" and not any(k in request for k in MUTATIONS[action]):
                    raise ServiceError("No article edits supplied")
                payload = encoded(request)
                if len(payload.encode()) > (MAX_MEDIA_EDIT if media else MAX_EDIT):
                    raise ServiceError("Review edit exceeds 16 MB with images" if media else "Review edit exceeds 2 MB", status=413)
                fingerprint = hashlib.sha256(payload.encode()).hexdigest()
                old = self.db.execute("SELECT request_hash FROM reviews WHERE id=?", (request_id,)).fetchone()
                if old:
                    if old[0] != fingerprint:
                        raise Conflict("A review request ID cannot be reused for different edits")
                else:
                    stored = self.db.execute("SELECT COUNT(*),COALESCE(SUM(request_bytes),0) FROM reviews").fetchone()
                    if stored[0] >= 10000 or stored[1] + len(payload.encode()) > 64_000_000:
                        raise ServiceError("Review receipt quota reached (10,000 operations / 64 MB); export and rotate the relay workspace", status=507)
                    current = self.snapshot(article_id)
                    state = "queued" if current["revision"] == revision else "conflict"
                    result = None if state == "queued" else encoded({"status": "refused", "reason": "Article changed on the Mac. Your submitted draft is preserved."})
                    now = self.clock.now()
                    self.db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?)",
                                    (request_id, article_id, payload, fingerprint, len(payload.encode()), state, result, now, now))
                return {"status": "queued", "operation": self.operation(request_id), **identity}
            raise ServiceError("Unsupported article action; publication and generation are not offered")
