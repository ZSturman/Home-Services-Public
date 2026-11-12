"""restic adapter over the existing repository.

This adapter never initializes, forgets, or prunes. Listing and download use
`--no-lock` so they never block the existing retention job's exclusive lock;
restore takes restic's normal non-exclusive lock so a concurrent prune cannot
remove data mid-restore. Only fixed argv lists are executed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

from ..config import Config, read_secret
from ..errors import CapabilityUnavailable, NotFound, ServiceError
from ..volumes import VolumeGuard

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{8,64}$")
_FRACTION = re.compile(r"(\.\d{6})\d+")


def snapshot_timestamp(value: str) -> float:
    """restic times carry nanoseconds and a UTC offset; Python 3.11 accepts
    at most microseconds."""
    return datetime.fromisoformat(_FRACTION.sub(r"\1", value).replace("Z", "+00:00")).timestamp()


class ResticError(ServiceError):
    status, code = 502, "restic_failed"


def safe_snapshot_id(value: str) -> str:
    if not _SNAPSHOT_ID.match(value or ""):
        raise ServiceError("snapshot id must be 8-64 lowercase hex characters", code="invalid_snapshot_id")
    return value


def safe_snapshot_path(value: str) -> str:
    if not value or "\x00" in value or not value.startswith("/"):
        raise ServiceError("snapshot path must be absolute", code="invalid_snapshot_path")
    p = PurePosixPath(value)
    if ".." in p.parts:
        raise ServiceError("snapshot path must not contain '..'", code="invalid_snapshot_path")
    return str(p)


class Restic:
    def __init__(self, config: Config, guard: VolumeGuard, *, role: str = "restic_repository"):
        self.config, self.guard = config, guard
        self.role = role
        self._cache_lock = threading.Lock()
        self._cache: tuple[float, list[dict]] | None = None

    # --- environment ------------------------------------------------------

    def repository_path(self) -> Path:
        return self.guard.root_path(self.role, write=False)

    @property
    def password_file(self) -> str:
        b = self.config.backup
        return b.companion_password_file if self.role == "companion_recovery" else b.password_file

    def _env(self) -> dict:
        b = self.config.backup
        if not b.enabled:
            raise CapabilityUnavailable("backups are not enabled on this host", details={"capability": "backups"})
        env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        env["RESTIC_REPOSITORY"] = str(self.repository_path())
        env["RESTIC_CACHE_DIR"] = str(self.config.data_dir / "restic-cache")
        if self.password_file:
            if read_secret(self.password_file, "") is None:
                raise CapabilityUnavailable("the repository password file is missing", code="repository_password_missing")
            env["RESTIC_PASSWORD_FILE"] = self.password_file
        elif self.role == "restic_repository" and b.password_env_var and os.environ.get(b.password_env_var):
            env["RESTIC_PASSWORD"] = os.environ[b.password_env_var]
        else:
            raise CapabilityUnavailable("no repository password is configured", code="repository_password_missing")
        return env

    def _run(self, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        env = self._env()
        try:
            proc = subprocess.run([self.config.backup.restic_binary, *args], env=env, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise CapabilityUnavailable("restic is not installed on this host", code="restic_missing") from None
        except subprocess.TimeoutExpired:
            raise ResticError("restic did not respond in time", code="restic_timeout") from None
        if proc.returncode != 0:
            raise ResticError("restic exited with an error", details={"exit_code": proc.returncode, "stderr": proc.stderr[-2000:]})
        return proc

    # --- read-only operations ---------------------------------------------

    def repository_status(self) -> dict:
        """Cheap status that never runs restic or takes a lock."""
        try:
            repo = self.repository_path()
        except ServiceError as exc:
            return {"state": "unavailable", "reason": exc.message, "details": exc.details}
        present = (repo / "config").is_file() and (repo / "data").is_dir()
        b = self.config.backup
        password = bool((self.password_file and Path(self.password_file).is_file()) or
                        (self.role == "restic_repository" and b.password_env_var and os.environ.get(b.password_env_var)))
        return {
            "state": "present" if present else "missing",
            "reason": "repository found" if present else "no restic repository at the configured root; Home Services never initializes one",
            "password_configured": password,
            "integrity": {"state": "not_owned", "reason": "integrity checks remain with the existing scheduled job during migration"},
            "retention": {"state": "not_owned", "reason": "forget/prune remain with the existing scheduled job during migration"},
        }

    def snapshots(self, *, max_age: float = 0) -> list[dict]:
        """All snapshots, newest first. `max_age` > 0 reuses a listing that
        is at most that many seconds old (status views poll; browsing and
        verification always list fresh)."""
        with self._cache_lock:
            if max_age and self._cache and time.monotonic() - self._cache[0] < max_age:
                return self._cache[1]
        proc = self._run(["snapshots", "--json", "--no-lock"], timeout=180)
        raw = json.loads(proc.stdout or "[]")
        out = []
        for s in raw:
            summary = s.get("summary")  # written by restic >= 0.17; older snapshots have none
            out.append({
                "id": s["id"], "short_id": s.get("short_id", s["id"][:8]), "time": s["time"],
                "hostname": s.get("hostname"), "paths": s.get("paths", []), "tags": s.get("tags") or [],
                "contents": None if not summary else {"files": summary.get("total_files_processed"),
                                                      "bytes": summary.get("total_bytes_processed")},
            })
        out.sort(key=lambda s: snapshot_timestamp(s["time"]), reverse=True)
        with self._cache_lock:
            self._cache = (time.monotonic(), out)
        return out

    def find_snapshot(self, snapshot_id: str) -> dict | None:
        """The repository's own record of a snapshot (full or short ID), or
        None if the repository does not contain it."""
        snap = safe_snapshot_id(snapshot_id)
        for s in self.snapshots():
            if s["id"] == snap or (len(snap) >= 8 and s["id"].startswith(snap)):
                return s
        return None

    def list_dir(self, snapshot_id: str, path: str) -> list[dict]:
        snap = safe_snapshot_id(snapshot_id)
        requested = safe_snapshot_path(path)
        proc = self._run(["ls", "--json", "--no-lock", snap, requested], timeout=300)
        entries = []
        found_self = requested == "/"
        for line in proc.stdout.splitlines():
            try:
                node = json.loads(line)
            except json.JSONDecodeError:
                continue
            if node.get("struct_type") != "node" and node.get("message_type") != "node":
                continue
            npath = node.get("path", "")
            if npath == requested:
                found_self = True
                if node.get("type") != "dir":
                    raise ServiceError("path is a file, not a directory", code="not_a_directory")
                continue
            if str(PurePosixPath(npath).parent) == requested:
                entries.append({"name": node.get("name"), "path": npath,
                                "kind": {"dir": "directory", "file": "file"}.get(node.get("type"), "other"),
                                "size": node.get("size") if node.get("type") == "file" else None,
                                "modified_at": node.get("mtime")})
        if not found_self:
            raise NotFound("path not found in snapshot", details={"path": requested})
        entries.sort(key=lambda e: e["name"] or "")
        return entries

    def node(self, snapshot_id: str, path: str) -> dict:
        parent = str(PurePosixPath(safe_snapshot_path(path)).parent)
        for e in self.list_dir(snapshot_id, parent):
            if e["path"] == path:
                return e
        raise NotFound("file not found in snapshot", details={"path": path})

    def open_dump(self, snapshot_id: str, path: str) -> subprocess.Popen:
        """Stream one archived regular file."""
        snap, p = safe_snapshot_id(snapshot_id), safe_snapshot_path(path)
        if self.node(snap, p)["kind"] != "file":
            raise ServiceError("only regular files can be downloaded; use a restore job for directories", code="not_a_file")
        return subprocess.Popen([self.config.backup.restic_binary, "dump", "--no-lock", snap, p],
                                env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    # --- restore ------------------------------------------------------------

    def restore(self, snapshot_id: str, includes: list[str], target: Path, *, on_progress, should_stop) -> dict:
        """Run `restic restore` into a fresh target. `on_progress(dict)` is
        called for each status line; `should_stop()` returning True
        terminates restic. Returns the summary message."""
        snap = safe_snapshot_id(snapshot_id)
        args = [self.config.backup.restic_binary, "restore", snap, "--target", str(target), "--json"]
        for inc in includes:
            args += ["--include", safe_snapshot_path(inc)]
        env = self._env()
        env["RESTIC_PROGRESS_FPS"] = "0.5"  # without a terminal restic reports only once a minute
        proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        summary: dict = {}
        stopped = threading.Event()
        finished = threading.Event()

        def watch():  # restic can be silent for long stretches; poll independently
            while not finished.wait(1.0):
                if should_stop():
                    stopped.set()
                    proc.terminate()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = msg.get("message_type")
            if kind == "status":
                on_progress({k: msg.get(k) for k in ("percent_done", "total_files", "files_restored", "total_bytes", "bytes_restored")})
            elif kind == "summary":
                summary = msg
        finished.set()
        watcher.join(timeout=5)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if stopped.is_set():
            return {"stopped": True}
        if proc.returncode != 0:
            err = proc.stderr.read()[-2000:] if proc.stderr else ""
            raise ResticError("restic restore failed", details={"exit_code": proc.returncode, "stderr": err})
        return summary
