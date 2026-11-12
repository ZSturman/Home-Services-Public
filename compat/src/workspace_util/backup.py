"""restic-backed backup engine.

Backup and sync are deliberately separate concepts (design principle #2/#3):
this module never talks to Syncthing, and Syncthing never calls into this
module. Every entry point that writes to the backup destination calls
mount_safety.require_mount first and fails loudly (never falls back to
writing on the SD card) if the backup HDD isn't actually mounted.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

from .config import BackupConfig, DiskConfig
from .mount_safety import MountSafetyError, require_mount


class BackupBlockedError(RuntimeError):
    """Raised when a backup/restore operation cannot proceed safely."""


class ResticNotFoundError(RuntimeError):
    pass


BACKUP_STATUS_FILENAME = "backup_status.json"
MAC_BACKUP_STATUS_FILENAME = "mac_backup_status.json"
ACTIVE_STATUS_MAX_AGE_SECONDS = 300
_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


def backup_status_path(logging_dir: str) -> Path:
    """Return the one persisted, client-facing status record for this host."""
    return Path(logging_dir) / BACKUP_STATUS_FILENAME


def mac_backup_status_path(logging_dir: str) -> Path:
    """Dedicated record for a Mac-originated job.

    Pi timers and Mac launchd jobs can run independently; they must never
    publish lifecycle state into the same record.
    """
    return Path(logging_dir) / MAC_BACKUP_STATUS_FILENAME


def _write_status(path: Path | None, payload: dict) -> None:
    """Atomically publish a status update without ever exposing a partial JSON
    record to a dashboard or kiosk client. Status telemetry must never make a
    backup fail, so filesystem errors here are deliberately non-fatal.
    """
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, sort_keys=True))
        temp.replace(path)
    except OSError:
        pass


def read_backup_status_file(path: Path, *, now: float | None = None) -> dict:
    """Read status defensively. An old active record is explicitly stale, not
    a claim that protection is still running after a process/service crash.
    """
    current = time.time() if now is None else now
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "state": "unknown", "active": False, "stage": "waiting",
            "updated_at": None, "warning": "No backup status has been recorded yet.",
        }
    updated_at = payload.get("updated_at")
    age_seconds = current - updated_at if isinstance(updated_at, (int, float)) else None
    payload["age_seconds"] = age_seconds
    if payload.get("active") and (age_seconds is None or age_seconds > ACTIVE_STATUS_MAX_AGE_SECONDS):
        payload.update({
            "state": "interrupted", "active": False, "stage": "interrupted",
            "stale_reason": "No supervisor heartbeat was received before the active-status deadline.",
            "warning": "Backup activity is stale; the previous process may have stopped unexpectedly.",
        })
    return payload


def read_backup_status(logging_dir: str, *, now: float | None = None) -> dict:
    return read_backup_status_file(backup_status_path(logging_dir), now=now)


def _restic_env(repo_path: str, password_env_var: str = "WORKSPACE_BACKUP_PASSWORD") -> dict:
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = repo_path
    if password_env_var not in env:
        raise BackupBlockedError(
            f"Backup password not set. Export {password_env_var} (see docs/security.md) "
            "before running any restic operation."
        )
    env["RESTIC_PASSWORD"] = env[password_env_var]
    return env


def _run_restic(args: list[str], repo_path: str, timeout: int = 3600) -> subprocess.CompletedProcess:
    try:
        env = _restic_env(repo_path)
    except BackupBlockedError:
        raise
    try:
        return subprocess.run(
            ["restic", *args], env=env, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ResticNotFoundError(
            "restic is not installed. See docs/backups.md for the install command."
        ) from exc


def ensure_backup_disk_mounted(disk_cfg: DiskConfig):
    """The single guard every backup/restore code path must call first."""
    try:
        return require_mount(disk_cfg.backup_mount, required_uuid=disk_cfg.backup_uuid or None, what="backup HDD")
    except MountSafetyError as exc:
        raise BackupBlockedError(
            f"BACKUP BLOCKED\nExpected backup volume is not mounted.\n{exc}"
        ) from exc


def init_repository(backup_cfg: BackupConfig, disk_cfg: DiskConfig) -> subprocess.CompletedProcess:
    ensure_backup_disk_mounted(disk_cfg)
    repo = backup_cfg.repository_path
    Path(backup_cfg.destination).mkdir(exist_ok=True)  # destination itself is the mounted volume root
    return _run_restic(["init"], repo)


def repository_exists(backup_cfg: BackupConfig, disk_cfg: DiskConfig) -> bool:
    try:
        ensure_backup_disk_mounted(disk_cfg)
    except BackupBlockedError:
        return False
    result = _run_restic(["cat", "config"], backup_cfg.repository_path, timeout=30)
    return result.returncode == 0


@dataclass
class BackupRunResult:
    success: bool
    started_at: float
    finished_at: float
    snapshot_id: str | None
    data_added_bytes: int | None
    files_new: int | None
    files_changed: int | None
    error: str | None
    stdout_tail: str
    stderr_tail: str


def run_backup(
    source_path: str,
    backup_cfg: BackupConfig,
    disk_cfg: DiskConfig,
    tags: list[str] | None = None,
    *,
    status_path: Path | None = None,
) -> BackupRunResult:
    started = time.time()
    state = {
        "state": "running", "active": True, "stage": "preparing",
        "started_at": started, "updated_at": started, "source": source_path,
        "files_processed": None, "files_total": None, "bytes_processed": None,
        "bytes_total": None, "percent_done": None, "rate_bytes_per_second": None,
        "snapshot_id": None, "error": None,
    }
    _write_status(status_path, state)
    try:
        ensure_backup_disk_mounted(disk_cfg)
    except BackupBlockedError as exc:
        state.update({"state": "failed", "active": False, "stage": "waiting_for_backup_disk", "error": str(exc), "updated_at": time.time()})
        _write_status(status_path, state)
        return BackupRunResult(
            success=False, started_at=started, finished_at=time.time(), snapshot_id=None,
            data_added_bytes=None, files_new=None, files_changed=None,
            error=str(exc), stdout_tail="", stderr_tail="",
        )

    args = ["backup", source_path, "--json"]
    for tag in (tags or []):
        args += ["--tag", tag]

    try:
        if status_path is None:
            result = _run_restic(args, backup_cfg.repository_path, timeout=6 * 3600)
            stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
        else:
            env = _restic_env(backup_cfg.repository_path)
            state.update({"stage": "backing_up", "updated_at": time.time()})
            _write_status(status_path, state)
            process = subprocess.Popen(
                ["restic", *args], env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
            )
            lines: list[str] = []
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("message_type") == "status":
                    # These fields are emitted by restic when known. Never
                    # synthesize a percentage from incomplete data.
                    state.update({
                        "files_processed": event.get("files_done"),
                        "files_total": event.get("total_files"),
                        "bytes_processed": event.get("bytes_done"),
                        "bytes_total": event.get("total_bytes"),
                        "percent_done": event.get("percent_done"),
                        "updated_at": time.time(),
                    })
                    _write_status(status_path, state)
            returncode = process.wait()
            stdout, stderr = "".join(lines), ""
    except (BackupBlockedError, ResticNotFoundError) as exc:
        state.update({"state": "failed", "active": False, "stage": "failed", "error": str(exc), "updated_at": time.time()})
        _write_status(status_path, state)
        return BackupRunResult(
            success=False, started_at=started, finished_at=time.time(), snapshot_id=None,
            data_added_bytes=None, files_new=None, files_changed=None,
            error=str(exc), stdout_tail="", stderr_tail="",
        )
    finished = time.time()

    snapshot_id = None
    data_added = None
    files_new = None
    files_changed = None
    if returncode == 0:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("message_type") == "summary":
                snapshot_id = event.get("snapshot_id")
                data_added = event.get("data_added")
                files_new = event.get("files_new")
                files_changed = event.get("files_changed")

    success = returncode == 0
    state.update({
        "state": "completed" if success else "failed", "active": False,
        "stage": "completed" if success else "failed", "updated_at": finished,
        "completed_at": finished, "snapshot_id": snapshot_id,
        "data_added_bytes": data_added, "files_new": files_new,
        "files_changed": files_changed,
        "error": None if success else stderr[-2000:] or stdout[-2000:],
    })
    _write_status(status_path, state)
    return BackupRunResult(
        success=success,
        started_at=started,
        finished_at=finished,
        snapshot_id=snapshot_id,
        data_added_bytes=data_added,
        files_new=files_new,
        files_changed=files_changed,
        error=None if success else stderr[-2000:] or stdout[-2000:],
        stdout_tail=stdout[-2000:],
        stderr_tail=stderr[-2000:],
    )


def apply_retention(backup_cfg: BackupConfig, disk_cfg: DiskConfig, prune: bool = True) -> subprocess.CompletedProcess:
    """`restic forget` using the configured hourly/daily/weekly/monthly keep
    counts. Never deletes data outside the configured repository."""
    ensure_backup_disk_mounted(disk_cfg)
    args = [
        "forget",
        "--keep-hourly", str(backup_cfg.hourly_keep),
        "--keep-daily", str(backup_cfg.daily_keep),
        "--keep-weekly", str(backup_cfg.weekly_keep),
        "--keep-monthly", str(backup_cfg.monthly_keep),
    ]
    if prune:
        args.append("--prune")
    return _run_restic(args, backup_cfg.repository_path, timeout=3600)


def list_snapshots(backup_cfg: BackupConfig, disk_cfg: DiskConfig) -> list[dict]:
    ensure_backup_disk_mounted(disk_cfg)
    result = _run_restic(["snapshots", "--json"], backup_cfg.repository_path, timeout=60)
    if result.returncode != 0:
        raise BackupBlockedError(result.stderr)
    return json.loads(result.stdout or "[]")


def _safe_snapshot_id(snapshot_id: str) -> str:
    """Accept only restic's hexadecimal snapshot IDs, never a selector or
    an argv-like value supplied by a browser."""
    value = str(snapshot_id or "").strip().lower()
    if not _SNAPSHOT_ID_RE.fullmatch(value):
        raise BackupBlockedError("Invalid backup snapshot ID.")
    return value


def _safe_snapshot_path(path: str) -> str:
    """Validate a path inside a snapshot's virtual filesystem.

    A snapshot is not a host filesystem root, but rejecting traversal and
    control characters still keeps this API's contract crisp and makes every
    restic invocation a fixed argv list.
    """
    value = str(path or "/").strip() or "/"
    if "\x00" in value:
        raise BackupBlockedError("Invalid backup path.")
    candidate = PurePosixPath(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise BackupBlockedError("Backup paths must be absolute and cannot contain '..'.")
    return str(candidate)


def list_snapshot_directory(
    snapshot_id: str, path: str, backup_cfg: BackupConfig, disk_cfg: DiskConfig,
) -> list[dict]:
    """List direct children of a restic snapshot directory.

    This reads from the encrypted repository only after the same UUID-backed
    HDD guard used by backup and restore operations.  The repository remains
    read-only from the dashboard's perspective.
    """
    snapshot = _safe_snapshot_id(snapshot_id)
    requested = _safe_snapshot_path(path)
    ensure_backup_disk_mounted(disk_cfg)
    result = _run_restic(["ls", "--json", snapshot, "--", requested], backup_cfg.repository_path, timeout=120)
    if result.returncode != 0:
        raise BackupBlockedError(result.stderr.strip() or "Could not read this backup snapshot.")

    target = PurePosixPath(requested)
    entries: list[dict] = []
    for line in result.stdout.splitlines():
        try:
            node = json.loads(line)
        except json.JSONDecodeError:
            continue
        if node.get("struct_type") != "node" or not isinstance(node.get("path"), str):
            continue
        try:
            node_path = PurePosixPath(_safe_snapshot_path(node["path"]))
        except BackupBlockedError:
            continue
        if node_path == target or node_path.parent != target:
            continue
        node_type = node.get("type")
        entries.append({
            "name": node.get("name") or node_path.name,
            "path": str(node_path),
            "is_dir": node_type == "dir",
            "size_bytes": None if node_type == "dir" else node.get("size"),
            "modified": node.get("mtime"),
        })
    return sorted(entries, key=lambda item: (not item["is_dir"], item["name"].lower()))


def open_snapshot_file(
    snapshot_id: str, path: str, backup_cfg: BackupConfig, disk_cfg: DiskConfig,
) -> subprocess.Popen:
    """Open one archived file as a binary restic dump process.

    The caller streams stdout directly to the authenticated dashboard user;
    materialising the whole file in Pi memory would make large restores unsafe.
    """
    snapshot = _safe_snapshot_id(snapshot_id)
    snapshot_path = _safe_snapshot_path(path)
    ensure_backup_disk_mounted(disk_cfg)
    try:
        return subprocess.Popen(
            ["restic", "dump", snapshot, snapshot_path],
            env=_restic_env(backup_cfg.repository_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise ResticNotFoundError(
            "restic is not installed. See docs/backups.md for the install command."
        ) from exc


def verify_integrity(backup_cfg: BackupConfig, disk_cfg: DiskConfig, read_data: bool = False) -> subprocess.CompletedProcess:
    ensure_backup_disk_mounted(disk_cfg)
    args = ["check"]
    if read_data:
        args.append("--read-data")
    return _run_restic(args, backup_cfg.repository_path, timeout=6 * 3600)


def restore_snapshot(
    snapshot_id: str,
    backup_cfg: BackupConfig,
    disk_cfg: DiskConfig,
    restore_root: str,
    include_path: str | None = None,
) -> subprocess.CompletedProcess:
    """Restore into a dated directory under restore_root -- NEVER the live
    workspace. include_path (if given) restricts restore to one project."""
    ensure_backup_disk_mounted(disk_cfg)
    target = Path(restore_root) / time.strftime("%Y-%m-%d")
    target.mkdir(parents=True, exist_ok=True)
    args = ["restore", snapshot_id, "--target", str(target)]
    if include_path:
        args += ["--include", include_path]
    return _run_restic(args, backup_cfg.repository_path, timeout=6 * 3600)
