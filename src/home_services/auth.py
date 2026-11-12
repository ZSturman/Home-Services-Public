"""Scoped API tokens and opaque media sessions.

Tokens look like `hst_<id>.<secret>`. Only a SHA-256 hash of the secret is
stored (the secret is 256 bits of randomness, so a slow KDF adds nothing).
Tokens are created/revoked with the admin CLI and shown exactly once.
"""
from __future__ import annotations

import hashlib
import hmac

from .db import Database, dumps, loads
from .errors import Forbidden, NotFound, Unauthenticated
from .runtime import Clock, IdFactory, iso

SCOPES = {
    "status:read": "Read authenticated service readiness and capability metadata",
    "articles:read": "Read cached article review copies and operation receipts",
    "articles:review": "Queue revision-bound article edits and explicit review decisions",
    "articles:sync": "Mac companion: synchronize snapshots and apply queued review receipts",
    "opportunities:read": "Read cached opportunity review copies and operation receipts",
    "opportunities:review": "Queue revision-bound opportunity decisions, drafts and schedule confirmations",
    "opportunities:sync": "Mac companion: publish opportunity copies and apply queued phone requests",
    "chewsense:deposit": "Chew Sense iPhone: deposit encrypted recordings and read their state and the status copy",
    "chewsense:collect": "Chew Sense Mac: download and acknowledge deposits, publish the status copy",
    "storage:read": "List volumes/roots, browse and download managed files",
    "storage:write": "Create directories, move to and restore from managed Trash",
    "transfers:write": "Create, append to, complete, and cancel upload sessions",
    "jobs:read": "Read durable job status",
    "jobs:write": "Create and cancel maintenance jobs",
    "backups:read": "Repository status, snapshot browsing, archived-file download",
    "backups:restore": "Create bounded restore jobs into fresh destinations",
    "backups:report": "Register a backup source and report runs (Files & Backups companion)",
    "cameras:read": "List cameras and their capabilities",
    "cameras:control": "Arm/disarm cameras and create live/media sessions",
    "media:read": "List legacy media and create media sessions",
    "notifications:admin": "Inspect the notification outbox and send test notifications",
    "legacy:storage-contract": "Read /api/storage/contract (Home Platform migration)",
}

PRESETS = {
    "home-page": ["status:read"],
    "content-manager": ["articles:read", "articles:review"],
    "article-companion": ["articles:read", "articles:sync"],
    "opportunity-review": ["opportunities:read", "opportunities:review"],
    "opportunity-companion": ["opportunities:read", "opportunities:sync"],
    "chewsense-phone": ["chewsense:deposit"],
    "chewsense-mac": ["chewsense:collect"],
    "camera-monitor": ["cameras:read", "cameras:control", "media:read", "jobs:read"],
    "files-backups": ["storage:read", "storage:write", "transfers:write", "jobs:read", "jobs:write", "backups:read", "backups:restore"],
    "files-backups-companion": ["backups:report", "backups:read", "jobs:read"],
    "home-platform-legacy": ["legacy:storage-contract"],
    "admin": sorted(SCOPES),
}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class Principal:
    def __init__(self, token_id: str, name: str, scopes: set[str]):
        self.token_id = token_id
        self.name = name
        self.scopes = scopes

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise Forbidden(f"token lacks the {scope} scope", details={"required_scope": scope})


class TokenStore:
    def __init__(self, db: Database, clock: Clock, ids: IdFactory):
        self.db, self.clock, self.ids = db, clock, ids

    def create(self, name: str, scopes: list[str], ttl_days: int | None = None) -> tuple[str, dict]:
        unknown = [s for s in scopes if s not in SCOPES]
        if unknown or not scopes:
            raise ValueError(f"unknown or empty scopes: {unknown}")
        token_id = self.ids.new("tok")
        secret = self.ids.secret()
        now = self.clock.now()
        expires = now + ttl_days * 86400 if ttl_days else None
        self.db.execute(
            "INSERT INTO tokens (id, name, secret_hash, scopes, created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (token_id, name, _hash(secret), dumps(sorted(scopes)), now, expires),
        )
        return f"hst_{token_id}.{secret}", self.describe(token_id)

    def describe(self, token_id: str) -> dict:
        row = self.db.one("SELECT * FROM tokens WHERE id=?", (token_id,))
        if row is None:
            raise NotFound("token not found")
        return {
            "id": row["id"], "name": row["name"], "scopes": loads(row["scopes"]),
            "created_at": iso(row["created_at"]), "expires_at": iso(row["expires_at"]),
            "revoked_at": iso(row["revoked_at"]), "last_used_at": iso(row["last_used_at"]),
        }

    def list(self) -> list[dict]:
        return [self.describe(r["id"]) for r in self.db.all("SELECT id FROM tokens ORDER BY created_at")]

    def revoke(self, token_id: str) -> dict:
        if self.db.execute("UPDATE tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (self.clock.now(), token_id)) == 0:
            self.describe(token_id)  # raises NotFound for unknown IDs
        return self.describe(token_id)

    def authenticate(self, presented: str | None) -> Principal:
        if not presented:
            raise Unauthenticated("missing bearer token")
        if not presented.startswith("hst_") or "." not in presented:
            raise Unauthenticated("malformed token")
        token_id, _, secret = presented[4:].partition(".")
        row = self.db.one("SELECT * FROM tokens WHERE id=?", (token_id,))
        # Compare even when the row is missing so timing does not reveal IDs.
        expected = row["secret_hash"] if row else _hash("absent")
        if not hmac.compare_digest(expected, _hash(secret)) or row is None:
            raise Unauthenticated("invalid token")
        now = self.clock.now()
        if row["revoked_at"] is not None:
            raise Unauthenticated("token revoked")
        if row["expires_at"] is not None and row["expires_at"] <= now:
            raise Unauthenticated("token expired")
        if row["last_used_at"] is None or now - row["last_used_at"] > 60:
            self.db.execute("UPDATE tokens SET last_used_at=? WHERE id=?", (now, token_id))
        return Principal(row["id"], row["name"], set(loads(row["scopes"])))


class MediaSessions:
    """Opaque expiring capability URLs for players that cannot send headers
    (AVPlayer). One session grants one resource; the session secret is never
    the API token, and only its hash is stored."""

    def __init__(self, db: Database, clock: Clock, ids: IdFactory, ttl_seconds: int):
        self.db, self.clock, self.ids, self.ttl = db, clock, ids, ttl_seconds

    # A live view keeps its session alive while the player keeps fetching the
    # playlist (sliding window), up to a hard cap.
    LIVE_IDLE_SECONDS = 120
    LIVE_MAX_SECONDS = 12 * 3600

    def create(self, kind: str, resource_id: str, principal: Principal, ttl: float | None = None) -> tuple[str, float]:
        secret = self.ids.secret()
        now = self.clock.now()
        expires = now + (ttl or self.ttl)
        self.db.execute("DELETE FROM media_sessions WHERE expires_at <= ?", (now,))
        self.db.execute(
            "INSERT INTO media_sessions (token_hash, resource_kind, resource_id, expires_at, created_at, created_by) VALUES (?,?,?,?,?,?)",
            (_hash(secret), kind, resource_id, expires, now, principal.token_id),
        )
        return secret, expires

    def resolve(self, secret: str, kind: str) -> str:
        found_kind, resource = self.lookup(secret)
        if found_kind != kind:
            raise NotFound("media session not found")
        return resource

    def lookup(self, secret: str) -> tuple[str, str]:
        """(kind, resource) of a live session; expired sessions are refused."""
        row = self.db.one("SELECT * FROM media_sessions WHERE token_hash=?", (_hash(secret),))
        if row is None:
            raise NotFound("media session not found")
        now = self.clock.now()
        if row["expires_at"] <= now:
            raise Unauthenticated("media session expired", code="media_session_expired")
        if row["resource_kind"] == "live_hls":
            extended = min(row["created_at"] + self.LIVE_MAX_SECONDS, now + self.LIVE_IDLE_SECONDS)
            if extended > row["expires_at"]:
                self.db.execute("UPDATE media_sessions SET expires_at=? WHERE token_hash=?", (extended, row["token_hash"]))
        return row["resource_kind"], row["resource_id"]
