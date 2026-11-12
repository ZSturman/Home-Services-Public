"""Standalone SQLite/token adapter for independent protocol-1 Pi backends."""
import argparse
import contextlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from types import SimpleNamespace

try:
    from .pairing_core import ConnectionStore, PairingFailure, Unauthorized, Missing, _hash, iso
except ImportError:
    from pairing_core import ConnectionStore, PairingFailure, Unauthorized, Missing, _hash, iso


class Database:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=15, check_same_thread=False, isolation_level=None)
        os.chmod(path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY,name TEXT NOT NULL,secret_hash TEXT NOT NULL,
          scopes TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL,revoked_at REAL,last_used_at REAL);""")
    def execute(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).rowcount
    def one(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()
    def all(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()
    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

class Tokens:
    def __init__(self, db, token_prefix):
        self.db, self.token_prefix = db, token_prefix
    def describe(self, identifier):
        row = self.db.one("SELECT * FROM tokens WHERE id=?", (identifier,))
        if row is None:
            raise Missing("credential not found")
        return {"id": row["id"], "name": row["name"], "scopes": json.loads(row["scopes"]),
                **{key: iso(row[key]) for key in ("created_at", "expires_at", "revoked_at", "last_used_at")}}
    def list(self):
        return [self.describe(r["id"]) for r in self.db.all("SELECT id FROM tokens ORDER BY created_at")]
    def authenticate(self, presented):
        identifier, _, secret = (presented[len(self.token_prefix):] if presented.startswith(self.token_prefix) else "").partition(".")
        row = self.db.one("SELECT * FROM tokens WHERE id=?", (identifier,))
        if not hmac.compare_digest(_hash(secret), row["secret_hash"] if row else _hash("absent")) or row is None:
            raise Unauthorized("invalid provider credential")
        if row["revoked_at"] is not None or (row["expires_at"] is not None and row["expires_at"] <= time.time()):
            raise Unauthorized("connection disconnected or expired")
        if row["last_used_at"] is None or time.time() - row["last_used_at"] > 60:
            self.db.execute("UPDATE tokens SET last_used_at=? WHERE id=?", (time.time(), identifier))
        return SimpleNamespace(token_id=identifier, scopes=set(json.loads(row["scopes"])))
    def revoke(self, identifier):
        self.describe(identifier)
        self.db.execute("UPDATE tokens SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (time.time(), identifier))

class SQLiteConnections(ConnectionStore):
    def __init__(self, path, *, provider, token_prefix, presets, permissions):
        self.provider, self.token_prefix = provider, token_prefix
        database = Database(path)
        database.execute("INSERT OR IGNORE INTO meta VALUES ('server_id',?)", (provider + "_" + secrets.token_hex(16),))
        context = SimpleNamespace(db=database, tokens=Tokens(database, token_prefix), clock=SimpleNamespace(now=time.time),
            ids=SimpleNamespace(new=lambda prefix: prefix + "_" + secrets.token_hex(8), secret=lambda: secrets.token_urlsafe(32)),
            server_id=database.one("SELECT value FROM meta WHERE key='server_id'")["value"])
        super().__init__(context, presets, permissions)

def route(connection, method, path, payload, bearer):
    if path in ("/api/v1/pairing/preview", "/api/v1/pairing/exchange") and method == "POST":
        allowed = {"invitation_id", "secret"} | ({"client_secret", "device_name"} if path.endswith("exchange") else set())
        if set(payload) != allowed or any(not isinstance(v, str) or len(v) > 128 for v in payload.values()):
            raise PairingFailure("invalid pairing request")
        proof = (payload["invitation_id"], payload["secret"])
        if path.endswith("preview"):
            return connection.preview(*proof)
        return connection.exchange(*proof, client_secret=payload["client_secret"], device_name=payload["device_name"])
    if path != "/api/v1/connection":
        raise Missing("connection endpoint not found")
    principal = connection.s.tokens.authenticate(bearer)
    current = connection.for_token(principal.token_id)
    if method == "GET":
        return current
    if method == "DELETE":
        return connection.revoke(current["id"])
    if method == "PATCH":
        if not set(payload) <= {"scopes", "preferences"} or ("scopes" in payload and
            (not isinstance(payload["scopes"], list) or len(payload["scopes"]) > 64 or any(not isinstance(s, str) for s in payload["scopes"]))):
            raise PairingFailure("invalid connection settings")
        if "preferences" in payload and not isinstance(payload["preferences"], dict):
            raise PairingFailure("preferences must be an object")
        return connection.update(current["id"], **payload)
    raise Missing("connection endpoint not found")

def manage(connection):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("catalog"); sub.add_parser("list")
    invite = sub.add_parser("invite")
    invite.add_argument("--preset", choices=connection.presets, required=True)
    invite.add_argument("--name", required=True); invite.add_argument("--base-url", required=True)
    invite.add_argument("--scope", action="append")
    for action in ("cancel", "revoke", "update"):
        child = sub.add_parser(action); child.add_argument("identifier")
        if action == "update":
            group = child.add_mutually_exclusive_group()
            group.add_argument("--scope", action="append"); group.add_argument("--disable-all", action="store_true")
            child.add_argument("--preferences-json")
    args = p.parse_args()
    try:
        if args.action == "invite":
            result = connection.invite(preset=args.preset, name=args.name, url=args.base_url, scopes=args.scope)
        elif args.action == "update":
            result = connection.update(args.identifier, scopes=[] if args.disable_all else args.scope,
                preferences=json.loads(args.preferences_json) if args.preferences_json else None)
        elif args.action in ("cancel", "revoke"):
            result = getattr(connection, args.action)(args.identifier)
        else:
            result = getattr(connection, args.action)()
        print(json.dumps(result))
    except PairingFailure as error:
        p.exit(1, error.message + "\n")

