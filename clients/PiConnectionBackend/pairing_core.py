"""One-use invitations and app connections layered over the existing token API.

No durable bearer credential travels in a QR code. The client supplies its own
random token secret so a lost exchange response can be retried without storing
recoverable secrets on the server. Only the same exchange can be replayed.
"""
from __future__ import annotations

import base64
import hmac
import re
from urllib.parse import urlsplit, urlunsplit

import hashlib
import json
from datetime import datetime, timezone

class PairingFailure(Exception):
    status, code = 400, "bad_request"
    def __init__(self, message, *, code=None, status=None):
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.status = status or self.status
    def as_dict(self):
        return {"error": {"code": self.code, "message": self.message}}
class Unauthorized(PairingFailure):
    status, code = 401, "unauthenticated"
class Denied(PairingFailure):
    status, code = 403, "forbidden"
class Missing(PairingFailure):
    status, code = 404, "not_found"
class Used(PairingFailure):
    status, code = 409, "conflict"
def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()
def dumps(value):
    return json.dumps(value, sort_keys=True)
def loads(value):
    return json.loads(value)
def iso(value):
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")

PROTOCOL = 1
INVITE_SECONDS = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS connection_invitations (
 id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, preset TEXT NOT NULL,
 name TEXT NOT NULL, scopes TEXT NOT NULL, base_url TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL,
 token_id TEXT, device_name TEXT, client_hash TEXT
);
CREATE TABLE IF NOT EXISTS app_connections (
 id TEXT PRIMARY KEY, token_id TEXT NOT NULL UNIQUE REFERENCES tokens(id),
 preset TEXT NOT NULL, allowed_scopes TEXT NOT NULL,
 device_name TEXT NOT NULL, preferences TEXT NOT NULL
);
"""


def base_url(value: str, *, development: bool = False, error=PairingFailure) -> str:
    try:
        u = urlsplit(value)
        _ = u.port
    except ValueError:
        raise error("use a valid HTTPS gateway URL", code="invalid_connection_url") from None
    local_http = development and u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}
    if (u.scheme != "https" and not local_http) or not u.hostname or u.username is not None or u.password is not None \
            or u.query or u.fragment or u.path not in {"", "/"} or any(c.isspace() for c in value):
        raise error("use an HTTPS gateway origin without credentials, path, query or fragment",
                           code="invalid_connection_url")
    return urlunsplit((u.scheme, u.netloc, "", "", ""))


class ConnectionStore:
    """Protocol-1 engine. Context supplies db, tokens, clock, ids and server_id.

    Backends retain their own SQLite/token stores and endpoint authorization.
    No networking, worker, filesystem or Home Services dependency lives here.
    """
    ServiceError, Unauthenticated = PairingFailure, Unauthorized
    Forbidden, NotFound, Conflict = Denied, Missing, Used
    provider, token_prefix = "home-services", "hst_"
    development = False
    def camera_choices(self):
        return []
    def _validate_preferences(self, preferences, allowed):
        if preferences:
            raise self.ServiceError("this provider has no folder or camera defaults", code="invalid_connection_preferences")

    def __init__(self, services, presets, permissions):
        self.presets, self.permissions = presets, permissions
        self.s = services
        self.db = services.db
        # Additive, rollback-compatible tables. No core or worker state changes.
        with self.db.lock:
            self.db.conn.executescript(SCHEMA)

    def catalog(self) -> dict:
        return {"protocol": PROTOCOL, "provider": self.provider, "server_id": self.s.server_id,
                "presets": [{"id": p, "scopes": scopes} for p, scopes in self.presets.items() if p != "admin"],
                "permissions": self.permissions, "invitation_ttl_seconds": INVITE_SECONDS,
                "cameras": self.camera_choices(),
                "selection_support": {"upload_folder": "client_preference", "camera_id": "client_preference"}}

    def invite(self, *, preset: str, name: str, url: str, scopes: list[str] | None = None) -> dict:
        if preset not in self.presets or preset == "admin":
            raise self.ServiceError("choose a registered app preset", code="invalid_connection_preset")
        if not name.strip() or len(name) > 120:
            raise self.ServiceError("connection name must be 1–120 characters", code="invalid_connection_name")
        scopes = sorted(set(self.presets[preset] if scopes is None else scopes))
        if not scopes or not set(scopes) <= set(self.presets[preset]):
            raise self.Forbidden("permissions must be a non-empty subset of the app preset")
        url = base_url(url, development=self.development, error=self.ServiceError)
        now = self.s.clock.now()
        secret, invitation_id = self.s.ids.secret(), self.s.ids.new("pair")
        with self.db.tx():
            self.db.execute("DELETE FROM connection_invitations WHERE expires_at < ?", (now - 86400,))
            if self.db.one("SELECT count(*) AS n FROM connection_invitations WHERE expires_at > ? AND revoked_at IS NULL AND token_id IS NULL", (now,))["n"] >= 100:
                raise self.ServiceError("too many pending invitations; cancel or let them expire", code="pairing_quota", status=429)
            self.db.execute("INSERT INTO connection_invitations (id,secret_hash,preset,name,scopes,base_url,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                            (invitation_id, _hash(secret), preset, name.strip(), dumps(scopes), url, now, now + INVITE_SECONDS))
        payload = {"protocol": PROTOCOL, "provider": self.provider, "base_url": url, "server_id": self.s.server_id,
                   "invitation_id": invitation_id, "secret": secret, "preset": preset, "expires_at": iso(now + INVITE_SECONDS)}
        encoded = base64.urlsafe_b64encode(dumps(payload).encode()).decode().rstrip("=")
        return {"invitation": payload, "pairing_uri": "homeservices://pair#" + encoded,
                "name": name.strip(), "scopes": scopes}

    def _invitation(self, invitation_id: str, secret: str):
        row = self.db.one("SELECT * FROM connection_invitations WHERE id=?", (invitation_id,))
        expected = row["secret_hash"] if row else _hash("absent")
        if not hmac.compare_digest(expected, _hash(secret)) or row is None:
            raise self.Unauthenticated("invalid pairing invitation")
        if row["revoked_at"] is not None or row["expires_at"] <= self.s.clock.now():
            raise self.Unauthenticated("pairing invitation expired or cancelled", code="pairing_expired")
        return row

    def preview(self, invitation_id: str, secret: str) -> dict:
        row = self._invitation(invitation_id, secret)
        if row["token_id"]:
            raise self.Conflict("this invitation has already been used", code="pairing_used")
        return {"server_id": self.s.server_id, "protocol": PROTOCOL, "preset": row["preset"], "name": row["name"],
                "scopes": loads(row["scopes"]), "expires_at": iso(row["expires_at"]), "base_url": row["base_url"]}

    def exchange(self, invitation_id: str, secret: str, *, client_secret: str, device_name: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", client_secret):
            raise self.ServiceError("client_secret must contain at least 32 random bytes encoded as base64url", code="invalid_client_secret")
        device_name = device_name.strip()
        if not device_name or len(device_name) > 120:
            raise self.ServiceError("device name must be 1–120 characters", code="invalid_device_name")
        with self.db.tx():
            row = self._invitation(invitation_id, secret)
            if row["token_id"]:
                if row["device_name"] != device_name or not hmac.compare_digest(row["client_hash"], _hash(client_secret)):
                    raise self.Conflict("this invitation has already been used", code="pairing_used")
                token_id = row["token_id"]
            else:
                token_id = self.s.ids.new("tok")
                self.db.execute("INSERT INTO tokens (id,name,secret_hash,scopes,created_at) VALUES (?,?,?,?,?)",
                                (token_id, row["name"], _hash(client_secret), row["scopes"], self.s.clock.now()))
                self.db.execute("INSERT INTO app_connections VALUES (?,?,?,?,?,?)",
                                (self.s.ids.new("conn"), token_id, row["preset"], row["scopes"], device_name, dumps({})))
                self.db.execute("UPDATE connection_invitations SET token_id=?,device_name=?,client_hash=? WHERE id=?",
                                (token_id, device_name, _hash(client_secret), invitation_id))
            token = f"{self.token_prefix}{token_id}.{client_secret}"
            # Retry cannot resurrect a revoked/expired credential.
            self.s.tokens.authenticate(token)
            return {"token": token, "base_url": row["base_url"], "server_id": self.s.server_id,
                    "protocol": PROTOCOL, "connection": self.for_token(token_id)}

    def for_token(self, token_id: str) -> dict:
        row = self.db.one("SELECT * FROM app_connections WHERE token_id=?", (token_id,))
        if row is None:
            raise self.NotFound("this token has no managed connection; existing API access remains available")
        return {"id": row["id"], "preset": row["preset"], "device_name": row["device_name"],
                "allowed_scopes": loads(row["allowed_scopes"]), "preferences": loads(row["preferences"]),
                "token": self.s.tokens.describe(token_id)}

    def list(self) -> dict:
        managed = self.db.all("SELECT token_id FROM app_connections ORDER BY id")
        managed_ids = {r["token_id"] for r in managed}
        invitations = self.db.all("SELECT id,preset,name,scopes,expires_at FROM connection_invitations WHERE token_id IS NULL AND revoked_at IS NULL AND expires_at > ?", (self.s.clock.now(),))
        return {"connections": [self.for_token(r["token_id"]) for r in managed],
                "existing_tokens": [t for t in self.s.tokens.list() if t["id"] not in managed_ids],
                "pending": [{"id": r["id"], "preset": r["preset"], "name": r["name"],
                             "scopes": loads(r["scopes"]), "expires_at": iso(r["expires_at"])} for r in invitations]}

    def adopt(self, token_id: str, preset: str) -> dict:
        """Attach management to an existing token, keeping its secret/scopes."""
        if preset not in self.presets or preset == "admin":
            raise self.ServiceError("choose a registered app preset", code="invalid_connection_preset")
        with self.db.tx():
            token = self.s.tokens.describe(token_id)
            expiry = self.db.one("SELECT expires_at FROM tokens WHERE id=?", (token_id,))["expires_at"]
            if token["revoked_at"] or (expiry is not None and expiry <= self.s.clock.now()) \
                    or not set(token["scopes"]) <= set(self.presets[preset]):
                raise self.Forbidden("the existing token must be active and fit the selected preset")
            if self.db.one("SELECT id FROM app_connections WHERE token_id=?", (token_id,)):
                raise self.Conflict("token already has a managed connection")
            self.db.execute("INSERT INTO app_connections VALUES (?,?,?,?,?,?)",
                            (self.s.ids.new("conn"), token_id, preset, dumps(token["scopes"]), token["name"], dumps({})))
        return self.for_token(token_id)

    def update(self, connection_id: str, *, scopes: list[str] | None = None, preferences: dict | None = None) -> dict:
        with self.db.tx():
            row = self.db.one("SELECT * FROM app_connections WHERE id=?", (connection_id,))
            if row is None:
                raise self.NotFound("connection not found")
            if scopes is not None:
                if not set(scopes) <= set(loads(row["allowed_scopes"])):
                    raise self.Forbidden("permissions cannot exceed those approved at pairing")
                self.db.execute("UPDATE tokens SET scopes=? WHERE id=?", (dumps(sorted(set(scopes))), row["token_id"]))
            if preferences is not None:
                self._validate_preferences(preferences, set(loads(row["allowed_scopes"])))
                self.db.execute("UPDATE app_connections SET preferences=? WHERE id=?", (dumps(preferences), connection_id))
        return self.for_token(row["token_id"])

    def revoke(self, connection_id: str) -> dict:
        row = self.db.one("SELECT token_id FROM app_connections WHERE id=?", (connection_id,))
        if row is None:
            raise self.NotFound("connection not found")
        self.s.tokens.revoke(row["token_id"])
        return self.for_token(row["token_id"])

    def cancel(self, invitation_id: str) -> dict:
        # A used invitation is not a connection revocation.
        if not self.db.execute("UPDATE connection_invitations SET revoked_at=? WHERE id=? AND token_id IS NULL",
                               (self.s.clock.now(), invitation_id)):
            raise self.NotFound("pending invitation not found")
        return {"id": invitation_id, "cancelled": True}
