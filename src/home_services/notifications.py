"""ntfy notification outbox.

Producers enqueue with a dedupe key (e.g. the motion event id), so a repeated
enqueue never sends twice. A separately supervised notifier process delivers
with exponential backoff; failures stay visible in the outbox until delivered
or until attempts are exhausted (`failed`).
"""
from __future__ import annotations

import httpx

from .config import Config, read_secret
from .db import Database, dumps, loads
from .errors import CapabilityUnavailable
from .runtime import Clock, IdFactory, iso

PRIORITIES = ("min", "low", "default", "high", "urgent")


class Outbox:
    def __init__(self, config: Config, db: Database, clock: Clock, ids: IdFactory):
        self.config, self.db, self.clock, self.ids = config, db, clock, ids

    def enqueue(self, dedupe_key: str, title: str, body: str, *, priority: str = "default", tags: list[str] | None = None) -> tuple[dict, bool]:
        if priority not in PRIORITIES:
            raise ValueError(priority)
        now = self.clock.now()
        msg_id = self.ids.new("ntf")
        inserted = self.db.execute(
            "INSERT OR IGNORE INTO outbox (id, dedupe_key, title, body, priority, tags, state, next_attempt_at, created_at)"
            " VALUES (?,?,?,?,?,?,'pending',?,?)", (msg_id, dedupe_key, title, body, priority, dumps(tags or []), now, now),
        )
        row = self.db.one("SELECT * FROM outbox WHERE dedupe_key=?", (dedupe_key,))
        return self._view(row), bool(inserted)

    def list(self, state: str | None = None, limit: int = 50) -> list[dict]:
        if state:
            rows = self.db.all("SELECT * FROM outbox WHERE state=? ORDER BY created_at DESC, id DESC LIMIT ?", (state, limit))
        else:
            rows = self.db.all("SELECT * FROM outbox ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
        return [self._view(r) for r in rows]

    def summary(self) -> dict:
        counts = {r["state"]: r["n"] for r in self.db.all("SELECT state, COUNT(*) AS n FROM outbox GROUP BY state")}
        return {"pending": counts.get("pending", 0), "delivered": counts.get("delivered", 0), "failed": counts.get("failed", 0)}

    def _view(self, row) -> dict:
        return {"id": row["id"], "dedupe_key": row["dedupe_key"], "title": row["title"], "priority": row["priority"],
                "tags": loads(row["tags"]), "state": row["state"], "attempts": row["attempts"],
                "next_attempt_at": iso(row["next_attempt_at"]) if row["state"] == "pending" else None,
                "last_error": row["last_error"], "created_at": iso(row["created_at"]), "delivered_at": iso(row["delivered_at"])}

    # --- delivery (notifier process) ---------------------------------------

    def ntfy_url(self) -> str:
        n = self.config.notifications
        url = read_secret(n.ntfy_url_file, n.ntfy_url_env_var) if n.enabled else None
        if not url:
            raise CapabilityUnavailable("ntfy delivery is not configured", code="notifications_unconfigured")
        return url

    def deliver_due(self, client: httpx.Client, url: str, limit: int = 20) -> dict:
        now = self.clock.now()
        rows = self.db.all("SELECT * FROM outbox WHERE state='pending' AND next_attempt_at <= ? ORDER BY next_attempt_at, id LIMIT ?", (now, limit))
        delivered = failed = 0
        for row in rows:
            headers = {"Title": row["title"], "Priority": row["priority"]}
            tags = loads(row["tags"])
            if tags:
                headers["Tags"] = ",".join(tags)
            error = None
            try:
                resp = client.post(url, content=row["body"].encode(), headers=headers, timeout=15)
                if resp.status_code >= 300:
                    error = f"ntfy returned HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}"  # never include the URL (it carries the private topic)
            attempts = row["attempts"] + 1
            if error is None:
                self.db.execute("UPDATE outbox SET state='delivered', attempts=?, delivered_at=?, last_error=NULL WHERE id=?",
                                (attempts, self.clock.now(), row["id"]))
                delivered += 1
            else:
                exhausted = attempts >= self.config.notifications.max_attempts
                backoff = min(3600, 15 * (2 ** (attempts - 1)))
                self.db.execute("UPDATE outbox SET state=?, attempts=?, last_error=?, next_attempt_at=? WHERE id=?",
                                ("failed" if exhausted else "pending", attempts, error, self.clock.now() + backoff, row["id"]))
                failed += 1
        return {"delivered": delivered, "retrying_or_failed": failed}
