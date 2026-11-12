"""SQLite state store on the application-data filesystem.

Holds configuration-adjacent state, tokens, jobs, transfers, backup runs,
and the notification outbox. Each process (gateway, each worker) opens its
own connection. WAL mode is refused on removable/exFAT-style filesystems.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS tokens (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, secret_hash TEXT NOT NULL,
  scopes TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL,
  revoked_at REAL, last_used_at REAL
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, queue TEXT NOT NULL, source TEXT,
  state TEXT NOT NULL, idempotency_key TEXT, request_hash TEXT,
  params TEXT NOT NULL, progress TEXT, result TEXT, error TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT, lease_expires_at REAL, heartbeat_at REAL, progress_at REAL, retry_at REAL,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL, created_by TEXT,
  UNIQUE (kind, idempotency_key)
);
CREATE INDEX IF NOT EXISTS jobs_queue_state ON jobs (queue, state, created_at);

CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY, root TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT, conflict TEXT NOT NULL, state TEXT NOT NULL, offset INTEGER NOT NULL,
  staging_name TEXT NOT NULL, idempotency_key TEXT UNIQUE, published_path TEXT, error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, created_by TEXT
);

CREATE TABLE IF NOT EXISTS trash (
  id TEXT PRIMARY KEY, root TEXT NOT NULL, original_path TEXT NOT NULL,
  trash_name TEXT NOT NULL, is_dir INTEGER NOT NULL, size INTEGER,
  deleted_at REAL NOT NULL, deleted_by TEXT
);

CREATE TABLE IF NOT EXISTS media_sessions (
  token_hash TEXT PRIMARY KEY, resource_kind TEXT NOT NULL, resource_id TEXT NOT NULL,
  expires_at REAL NOT NULL, created_at REAL NOT NULL, created_by TEXT
);

CREATE TABLE IF NOT EXISTS backup_sources (
  id TEXT PRIMARY KEY, host TEXT NOT NULL, path TEXT NOT NULL, tag TEXT NOT NULL,
  scheduler TEXT NOT NULL, registered_at REAL NOT NULL, registered_by TEXT,
  UNIQUE (host, path)
);

CREATE TABLE IF NOT EXISTS backup_runs (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES backup_sources(id),
  state TEXT NOT NULL, idempotency_key TEXT UNIQUE, lease_expires_at REAL,
  started_at REAL NOT NULL, heartbeat_at REAL, progress_at REAL, progress TEXT,
  snapshot_id TEXT, summary TEXT, error TEXT, finished_at REAL
);

CREATE TABLE IF NOT EXISTS repository_leases (
  repository TEXT PRIMARY KEY, holder_kind TEXT NOT NULL, holder_id TEXT NOT NULL,
  acquired_at REAL NOT NULL, expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
  body TEXT NOT NULL, priority TEXT NOT NULL, tags TEXT NOT NULL, state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
  last_error TEXT, created_at REAL NOT NULL, delivered_at REAL
);

CREATE TABLE IF NOT EXISTS camera_state (
  camera_id TEXT PRIMARY KEY, armed INTEGER NOT NULL, updated_at REAL NOT NULL
);

-- v2: the native HLS recorder. A chunk is about a minute of the camera's own
-- MPEG-TS segments concatenated byte-for-byte (no transcoding); path is
-- relative to the recordings root.
CREATE TABLE IF NOT EXISTS camera_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT NOT NULL, start_ts REAL NOT NULL,
  duration REAL NOT NULL, path TEXT NOT NULL UNIQUE, bytes INTEGER NOT NULL,
  motion_frames INTEGER NOT NULL DEFAULT 0, discontinuity INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS camera_chunks_time ON camera_chunks (camera_id, start_ts);

CREATE TABLE IF NOT EXISTS camera_events (
  id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, started_at REAL NOT NULL, last_motion_at REAL NOT NULL,
  ended_at REAL, end_reason TEXT, notified INTEGER NOT NULL DEFAULT 0, protected INTEGER NOT NULL DEFAULT 0,
  deleted_at REAL, delete_reason TEXT
);
CREATE INDEX IF NOT EXISTS camera_events_time ON camera_events (camera_id, started_at);

CREATE TABLE IF NOT EXISTS camera_status (
  camera_id TEXT PRIMARY KEY, recorder_heartbeat_at REAL, last_segment_at REAL,
  state TEXT, detail TEXT, updated_at REAL
);
"""


class DatabaseLocationError(RuntimeError):
    pass


class Database:
    """One SQLite connection per process, serialized by a lock so the
    gateway's thread pool never interleaves transactions."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        row = self.one("SELECT value FROM meta WHERE key='schema_version'")
        if row is not None and int(row["value"]) > SCHEMA_VERSION:
            raise DatabaseLocationError(f"database schema {row['value']} is newer than this release ({SCHEMA_VERSION}); roll forward instead")
        # v3: backup sources may live in an external repository Home Services cannot read.
        if "repository" not in {r["name"] for r in self.all("PRAGMA table_info(backup_sources)")}:
            self.execute("ALTER TABLE backup_sources ADD COLUMN repository TEXT NOT NULL DEFAULT 'primary'")
        # Migrations only add tables and columns, so an older release still runs on a newer database.
        self.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (str(SCHEMA_VERSION),))

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, params).rowcount

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    @contextmanager
    def tx(self):
        """BEGIN IMMEDIATE ... COMMIT, rolled back on any exception."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def get_meta(self, key: str) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def dumps(value) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def loads(value: str | None):
    return None if value is None else json.loads(value)
