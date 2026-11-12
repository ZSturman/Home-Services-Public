"""Mobile-friendly workspace dashboard.

Served over Tailscale only (see net.resolve_bind_host / docs/security.md).
Every filesystem-touching route resolves its path through
paths.resolve_within_roots against config.dashboard.allowed_roots before
doing anything else -- see Phase 8's traversal-protection requirement.
"""
from __future__ import annotations

import json
import hmac
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from .. import backup as backup_mod
from .. import camera as camera_mod
from .. import camera_sync
from .. import camera_stream
from .. import motion as motion_mod
from ..camera_service import CameraService
from ..commands import CommandHistory, CommandNotAllowedError, run_whitelisted_command
from ..config import Config, load_config
from ..doctor import overall_status, run_doctor
from ..files import guess_mimetype, list_directory, read_text_preview
from ..git_scan import scan_repo
from ..mount_safety import check_mount
from ..net import resolve_bind_host
from ..paths import PathNotAllowedError, resolve_within_roots
from ..projects import discover_projects, human_age
from ..storage_browser import (
    StorageUnavailableError,
    list_storage_directory,
    resolve_storage_path,
    storage_status,
)
from ..tapo import TapoService
from ..workspace_state import WorkspaceMode, build_storage_contract, resolve_workspace_state
from .ui import INDEX_HTML

_config: Config | None = None
_camera_service: CameraService | None = None
_tapo_service: TapoService | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def get_camera_service() -> CameraService:
    """Lazily create the single, process-wide camera background service.

    Guarded so a second call (e.g. from a test re-entering the app) never
    starts a second capture loop -- see docs/camera.md#service-architecture
    on why duplicate loops must be impossible even under misuse.
    """
    global _camera_service
    if _camera_service is None:
        _camera_service = CameraService(get_config())
    return _camera_service


def get_tapo_service() -> TapoService:
    """Lazily create the single, process-wide Tapo polling service -- same
    one-instance-per-process guard as get_camera_service()."""
    global _tapo_service
    if _tapo_service is None:
        _tapo_service = TapoService(get_config())
    return _tapo_service


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if get_config().camera.enabled and get_config().camera.background_capture_enabled:
        get_camera_service().start()
    if get_config().tapo.enabled:
        get_tapo_service().start()
    yield
    if _camera_service is not None:
        _camera_service.stop()
    if _tapo_service is not None:
        _tapo_service.stop()


app = FastAPI(title="Workspace Dashboard", lifespan=_lifespan)
# The status record protects consumers across restarts; this lock closes the
# small same-process race between two dashboard POSTs before either worker has
# written its initial status record.
_dashboard_backup_lock = threading.Lock()
_shutdown_lock = threading.Lock()
_shutdown_cancel_event: threading.Event | None = None
_shutdown_requested_at: float | None = None
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_AUTOMATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SHUTDOWN_CONFIRMATION = "SHUT DOWN PI"
_SESSION_COOKIE_NAME = "workspace_session"


def _require_auth(request: Request, cfg: Config) -> None:
    expected = os.environ.get(cfg.dashboard.auth_token_env_var)
    if not expected:
        return  # no token configured -- Tailscale is the boundary; see docs/security.md
    provided = request.headers.get("X-Workspace-Token") or request.cookies.get(_SESSION_COOKIE_NAME, "")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="missing or invalid X-Workspace-Token")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in {"/api/health", "/api/session"}:
        try:
            _require_auth(request, get_config())
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/session")
async def create_session(request: Request):
    """Exchange the typed dashboard token for an HttpOnly HTTPS cookie.

    A normal download link and Safari's native HLS player cannot attach the
    prior JavaScript-only header.  Keeping the credential in an HttpOnly,
    same-site Secure cookie lets both stream normally without adding a token
    to a URL, browser history, or frontend source.
    """
    cfg = get_config()
    expected = os.environ.get(cfg.dashboard.auth_token_env_var)
    if expected:
        presented = request.headers.get("X-Workspace-Token", "")
        if not hmac.compare_digest(presented, expected):
            raise HTTPException(status_code=401, detail="invalid dashboard token")
    response = JSONResponse({"authenticated": True})
    if expected:
        response.set_cookie(
            _SESSION_COOKIE_NAME,
            expected,
            max_age=60 * 60 * 24 * 30,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
    return response


@app.get("/api/camera/status")
def camera_status_api():
    status = get_camera_service().status_dict()
    status["live_stream"] = camera_stream.stream_status(get_config().camera)
    return status


@app.get("/api/camera/live/status")
def camera_live_status_api():
    return camera_stream.stream_status(get_config().camera)


@app.get("/api/camera/live/{asset_name}")
def camera_live_asset_api(asset_name: str):
    """Serve the short-lived HLS playlist or segment after dashboard auth."""
    if asset_name != camera_stream.PLAYLIST_NAME and not re.fullmatch(r"segment\d{6}\.ts", asset_name):
        raise HTTPException(status_code=404, detail="stream asset not found")
    root = camera_stream.hls_directory(get_config().camera)
    try:
        asset = (root / asset_name).resolve()
        asset.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="stream asset not found") from None
    if not asset.is_file():
        raise HTTPException(status_code=404, detail="stream is starting or reconnecting")
    media_type = "application/vnd.apple.mpegurl" if asset_name.endswith(".m3u8") else "video/mp2t"
    cache_control = "no-store, max-age=0" if asset_name.endswith(".m3u8") else "private, max-age=2"
    return FileResponse(asset, media_type=media_type, headers={"Cache-Control": cache_control})


@app.get("/api/camera/snapshot")
def camera_snapshot_api():
    """On-demand single-frame capture (not persisted). See /api/camera/latest
    for the cheap, always-available most-recent *persisted* snapshot."""
    service = get_camera_service()
    try:
        image = service.capture_on_demand()
    except camera_mod.CameraBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except camera_mod.CameraCaptureError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/camera/latest")
def camera_latest_api():
    """The most recently *persisted* snapshot -- cheap (no device access),
    safe to poll frequently from the dashboard's live camera card."""
    service = get_camera_service()
    if not service.last_snapshot_path:
        raise HTTPException(status_code=404, detail="no snapshot has been captured yet")
    path = Path(service.last_snapshot_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="the most recent snapshot is no longer on disk")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/camera/storage")
def camera_storage_api():
    return camera_mod.storage_usage(get_config().camera).as_dict()


@app.get("/api/camera/sync")
def camera_sync_api():
    return camera_sync.folder_sync_status(get_config())


@app.get("/api/camera/out-of-house")
def out_of_house_status_api():
    return {"armed": get_camera_service().armed}


@app.post("/api/camera/out-of-house")
def out_of_house_set_api(payload: dict):
    armed = bool(payload.get("armed"))
    get_camera_service().arm(armed)
    return {"armed": armed}


@app.get("/api/camera/snapshots")
def camera_snapshots_list_api(date: str | None = None, limit: int = 60):
    """Snapshot history, grouped by day. Without `date`, lists available
    days (newest first) so the UI can lazily drill in rather than loading
    every snapshot ever taken."""
    cfg = get_config()
    root = Path(cfg.camera.storage_dir) / camera_mod.SNAPSHOTS_SUBDIR
    if not root.exists():
        return {"days": [], "snapshots": []}
    if date is None:
        days = sorted((d.name for d in root.iterdir() if d.is_dir()), reverse=True)
        return {"days": days, "snapshots": []}
    try:
        day_dir = resolve_within_roots(str(root / date), [str(root)])
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=400, detail="invalid date") from exc
    if not day_dir.is_dir():
        return {"days": [], "snapshots": []}
    files = sorted((f for f in day_dir.glob("*.jpg")), reverse=True)[:max(1, min(limit, 500))]
    return {
        "days": [],
        "snapshots": [
            {"path": f"{date}/{f.name}", "size_bytes": f.stat().st_size, "modified": f.stat().st_mtime}
            for f in files
        ],
    }


@app.get("/api/camera/snapshots/file")
def camera_snapshot_file_api(path: str):
    cfg = get_config()
    try:
        resolved = camera_mod.resolve_snapshot_path(cfg.camera, path)
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="snapshot not found")
    return FileResponse(resolved, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/camera/events")
def camera_events_list_api(limit: int = 30):
    cfg = get_config()
    events_root = Path(cfg.camera.storage_dir) / camera_mod.EVENTS_SUBDIR
    return motion_mod.list_events(events_root, limit=max(1, min(limit, 200)))


@app.get("/api/camera/events/{event_id}")
def camera_event_detail_api(event_id: str):
    cfg = get_config()
    try:
        event_dir = camera_mod.resolve_event_path(cfg.camera, event_id)
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        meta = motion_mod.read_event_meta(event_dir)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="event not found")
    return meta


@app.get("/api/camera/events/{event_id}/media/{filename}")
def camera_event_media_api(event_id: str, filename: str):
    """Serves one file (recording.mp4, a pre-context frame, ...) from within
    a single event's own directory only -- resolve_event_path rejects any
    filename that would escape it."""
    cfg = get_config()
    try:
        path = camera_mod.resolve_event_path(cfg.camera, event_id, filename)
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type=guess_mimetype(path), headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/status")
def status():
    cfg = get_config()
    results = run_doctor(cfg)

    workspace_usage = None
    try:
        u = shutil.disk_usage(cfg.disk.workspace_mount)
        workspace_usage = {"used": u.used, "total": u.total}
    except FileNotFoundError:
        pass

    backup_usage = None
    try:
        u = shutil.disk_usage(cfg.disk.backup_mount)
        backup_usage = {"used": u.used, "total": u.total}
    except FileNotFoundError:
        pass

    snapshots_count = None
    last_backup = None
    if backup_mod.repository_exists(cfg.backup, cfg.disk):
        try:
            snaps = backup_mod.list_snapshots(cfg.backup, cfg.disk)
            snapshots_count = len(snaps)
            if snaps:
                last_backup = max(snaps, key=lambda s: s["time"])["time"]
        except backup_mod.BackupBlockedError:
            pass

    ws_state = resolve_workspace_state(cfg)

    return {
        "overall": overall_status(results),
        "checks": [{"name": r.name, "status": r.status, "detail": r.detail} for r in results],
        "storage": {"workspace": workspace_usage, "backup": backup_usage},
        "backup": {
            "snapshots": snapshots_count,
            "last": last_backup,
            "status": backup_mod.read_backup_status(cfg.logging.dir),
        },
        "workspace_state": {
            "mode": ws_state.mode.value,
            "active_path": ws_state.active_path,
            "projects_path": str(Path(ws_state.active_path) / cfg.workspace.projects_subdir) if ws_state.is_active else None,
            "detail": ws_state.detail,
        },
        "system_control": _shutdown_status(cfg),
        "camera": get_camera_service().status_dict() if cfg.camera.enabled else {"enabled": False},
    }


def _shutdown_status(cfg: Config) -> dict:
    """Return request state; no general system-command API exists here."""
    with _shutdown_lock:
        pending = _shutdown_cancel_event is not None
        requested_at = _shutdown_requested_at
    return {
        "shutdown_enabled": cfg.dashboard.shutdown_enabled,
        "pending": pending,
        "requested_at": requested_at,
        "delay_seconds": max(5, cfg.dashboard.shutdown_delay_seconds),
    }


def _shutdown_when_safe(delay_seconds: int, cancel_event: threading.Event) -> None:
    """Wait for the response/cancellation window, then invoke one fixed action."""
    global _shutdown_cancel_event, _shutdown_requested_at
    if cancel_event.wait(delay_seconds):
        return
    # Mark this request as committed before handing control to systemd, so a
    # second browser request cannot mistake it for a cancelable action.
    with _shutdown_lock:
        if _shutdown_cancel_event is not cancel_event:
            return
        _shutdown_cancel_event = None
        _shutdown_requested_at = None
    # `-n` guarantees this cannot hang asking for a password. The exact argv
    # list is intentionally the entire privilege boundary: no user input ever
    # becomes a command, flag, or systemd unit name.
    subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "poweroff"], check=False, timeout=20)


@app.get("/api/system/shutdown")
def shutdown_status_api():
    return _shutdown_status(get_config())


@app.post("/api/system/shutdown", status_code=202)
def shutdown_api(payload: dict):
    """Schedule a cancellable, explicit Pi power-off after safety checks."""
    global _shutdown_cancel_event, _shutdown_requested_at
    cfg = get_config()
    if not cfg.dashboard.shutdown_enabled:
        raise HTTPException(status_code=403, detail="Pi shutdown is disabled in configuration.")
    if payload.get("confirmation") != _SHUTDOWN_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f'Type "{_SHUTDOWN_CONFIRMATION}" to confirm shutdown.')

    pi_status = backup_mod.read_backup_status(cfg.logging.dir)
    mac_status = backup_mod.read_backup_status_file(backup_mod.mac_backup_status_path(cfg.logging.dir))
    if pi_status.get("active") or mac_status.get("active"):
        raise HTTPException(status_code=409, detail="A backup is active. Wait for it to finish before shutting down the Pi.")

    delay_seconds = max(5, cfg.dashboard.shutdown_delay_seconds)
    with _shutdown_lock:
        if _shutdown_cancel_event is not None:
            raise HTTPException(status_code=409, detail="A Pi shutdown is already scheduled.")
        cancel_event = threading.Event()
        _shutdown_cancel_event = cancel_event
        _shutdown_requested_at = time.time()
    threading.Thread(
        target=_shutdown_when_safe, args=(delay_seconds, cancel_event), daemon=True,
        name="dashboard-pi-shutdown",
    ).start()
    return {"accepted": True, "shutdown_in_seconds": delay_seconds, "cancel_url": "/api/system/shutdown/cancel"}


@app.post("/api/system/shutdown/cancel")
def cancel_shutdown_api():
    global _shutdown_cancel_event, _shutdown_requested_at
    with _shutdown_lock:
        if _shutdown_cancel_event is None:
            raise HTTPException(status_code=409, detail="No cancellable Pi shutdown is scheduled.")
        _shutdown_cancel_event.set()
        _shutdown_cancel_event = None
        _shutdown_requested_at = None
    return {"cancelled": True}


@app.get("/api/backup/status")
def backup_status_api():
    """Shared backup lifecycle/status contract for kiosk and phone clients.
    The backup engine alone writes it; clients only observe it."""
    cfg = get_config()
    payload = backup_mod.read_backup_status(cfg.logging.dir)
    mac_payload = backup_mod.read_backup_status_file(backup_mod.mac_backup_status_path(cfg.logging.dir))
    # An active (or terminal) Mac job is a distinct source from the Pi's
    # scheduled local backup. Prefer it while it is relevant, never merge its
    # fields with the Pi record.
    if mac_payload.get("state") in {"preparing", "running", "completed", "interrupted", "failed"}:
        payload = mac_payload
    now = time.time()
    heartbeat_at = payload.get("last_heartbeat_at") or payload.get("updated_at")
    progress_at = payload.get("last_progress_at")
    heartbeat_age = now - heartbeat_at if isinstance(heartbeat_at, (int, float)) else None
    progress_age = now - progress_at if isinstance(progress_at, (int, float)) else None
    payload["process"] = {
        "state": "alive" if payload.get("active") and heartbeat_age is not None and heartbeat_age <= backup_mod.ACTIVE_STATUS_MAX_AGE_SECONDS else "unknown",
        "pid": payload.get("pid"), "last_heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age, "observer": payload.get("supervisor"),
    }
    payload["progress"] = {
        "state": "current" if payload.get("metrics_fresh") and progress_age is not None and progress_age <= backup_mod.ACTIVE_STATUS_MAX_AGE_SECONDS else "stale" if progress_at else "unavailable",
        "last_event_at": progress_at, "age_seconds": progress_age,
        "files_processed": payload.get("files_processed"), "files_total": payload.get("files_total"),
        "bytes_processed": payload.get("bytes_processed"), "bytes_total": payload.get("bytes_total"),
        "percent_done": payload.get("percent_done"),
    }
    return payload


@app.get("/api/backup/destination")
def backup_destination_api():
    """Cheap physical-destination contract for dashboard consumers.

    This intentionally does *not* run restic or inspect snapshots: querying
    destination capacity must stay fast while a large backup owns the repo.
    """
    cfg = get_config()
    mount = check_mount(cfg.disk.backup_mount, required_uuid=cfg.disk.backup_uuid or None)
    usage = None
    if mount.mounted:
        try:
            disk_usage = shutil.disk_usage(cfg.disk.backup_mount)
            usage = {"total_bytes": disk_usage.total, "used_bytes": disk_usage.used, "available_bytes": disk_usage.free}
        except OSError:
            pass
    return {
        "state": "mounted" if mount.mounted else "unavailable", "device": mount.device,
        "uuid": cfg.disk.backup_uuid or None, "mount": cfg.disk.backup_mount,
        "filesystem": mount.fstype, "repository": cfg.backup.repository_path,
        "usage": usage,
        "repository_state": "not_checked", # restic checks belong to the scheduled check job, never this hot path.
    }


@app.get("/api/storage/contract")
def storage_contract_api():
    """The stable, narrow contract other projects on this Pi (e.g.
    Bidirectional Project Hub / home-platform) should consult to learn
    whether the real workspace is available -- instead of reading /dev,
    /etc/fstab, mount tables, or this project's config/state directly. See
    workspace_state.build_storage_contract for field semantics."""
    return build_storage_contract(get_config()).as_dict()


@app.get("/api/storage/status")
def attached_storage_status_api():
    """Status of the one read-only HDD browser root."""
    return storage_status(get_config())


@app.get("/api/storage/list")
def attached_storage_list_api(path: str = "", limit: int = 120, cursor: str | None = None):
    """List a bounded, read-only page under the UUID-verified backup HDD."""
    try:
        return list_storage_directory(get_config(), path, limit=limit, cursor=cursor)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PathNotAllowedError:
        raise HTTPException(status_code=403, detail="path is outside the attached storage root") from None
    except (NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/storage/download")
def attached_storage_download_api(path: str):
    """Stream one verified HDD file; Starlette preserves HTTP Range support."""
    try:
        resolved = resolve_storage_path(get_config(), path)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PathNotAllowedError:
        raise HTTPException(status_code=403, detail="path is outside the attached storage root") from None
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        resolved,
        media_type=guess_mimetype(resolved),
        filename=resolved.name,
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/projects")
def projects_api():
    cfg = get_config()
    state = resolve_workspace_state(cfg)
    if not state.is_active:
        return []
    root = Path(state.active_path) / cfg.workspace.projects_subdir
    out = []
    for p in discover_projects(root):
        g = p.git
        out.append({
            "name": p.name,
            "branch": g.branch,
            "is_repo": g.is_repo,
            "clean": g.clean,
            "modified_count": g.modified_count,
            "untracked_count": g.untracked_count,
            "last_modified": p.last_modified,
            "last_modified_human": human_age(p.last_modified),
        })
    return out


@app.get("/api/projects/{name}")
def project_detail_api(name: str):
    cfg = get_config()
    state = resolve_workspace_state(cfg)
    if not state.is_active:
        raise HTTPException(status_code=503, detail="no active workspace path -- workspace disk pending")
    root = Path(state.active_path) / cfg.workspace.projects_subdir
    try:
        path = resolve_within_roots(str(root / name), [str(root)])
    except PathNotAllowedError:
        raise HTTPException(status_code=400, detail="invalid project name")
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="project not found")
    git = scan_repo(path)
    entries = list_directory(path)
    return {
        "name": name,
        "path": str(path),
        "branch": git.branch,
        "clean": git.clean,
        "modified_count": git.modified_count,
        "untracked_count": git.untracked_count,
        "warnings": git.warnings,
        "files": [{"name": e.name, "is_dir": e.is_dir, "size_bytes": e.size_bytes} for e in entries],
    }


def _allowed_roots(cfg: Config) -> list[str]:
    return cfg.dashboard.allowed_roots


def _safe_folder_name(name: str) -> str:
    """Validate a single directory name before joining it to an approved
    browser path.  Folder creation must never be a way around the roots gate."""
    value = str(name or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise HTTPException(status_code=400, detail="folder name must be one simple name")
    return value


@app.get("/api/files")
def files_list(path: str):
    cfg = get_config()
    try:
        resolved = resolve_within_roots(path, _allowed_roots(cfg))
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    try:
        entries = list_directory(resolved)
    except NotADirectoryError:
        raise HTTPException(status_code=400, detail="not a directory")
    return [{"name": e.name, "path": e.path, "is_dir": e.is_dir, "size_bytes": e.size_bytes, "modified": e.modified} for e in entries]


@app.get("/api/files/content")
def files_content(path: str):
    cfg = get_config()
    try:
        resolved = resolve_within_roots(path, _allowed_roots(cfg))
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    try:
        content = read_text_preview(resolved)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"path": content.path, "text": content.text, "truncated": content.truncated}


@app.get("/api/files/download")
def files_download(path: str):
    cfg = get_config()
    try:
        resolved = resolve_within_roots(path, _allowed_roots(cfg))
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(resolved, media_type=guess_mimetype(resolved), filename=resolved.name)


@app.post("/api/files/upload")
async def files_upload(dir_path: str, file: UploadFile):
    cfg = get_config()
    try:
        resolved_dir = resolve_within_roots(dir_path, _allowed_roots(cfg))
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not resolved_dir.is_dir():
        raise HTTPException(status_code=400, detail="target directory does not exist")
    # Filename itself must not smuggle a traversal ('../../etc/passwd').
    safe_name = Path(file.filename or "").name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    dest = resolved_dir / safe_name
    try:
        dest.relative_to(resolved_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid filename")
    try:
        with open(dest, "xb") as f:
            while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                f.write(chunk)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="a file with that name already exists") from exc
    finally:
        await file.close()
    return {"saved": str(dest)}


@app.post("/api/files/folders", status_code=201)
def files_create_folder(dir_path: str, name: str):
    cfg = get_config()
    try:
        resolved_dir = resolve_within_roots(dir_path, _allowed_roots(cfg))
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not resolved_dir.is_dir():
        raise HTTPException(status_code=400, detail="target directory does not exist")
    destination = resolved_dir / _safe_folder_name(name)
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="a file or folder with that name already exists") from exc
    return {"created": str(destination)}


@app.get("/api/commands")
def commands_list():
    cfg = get_config()
    return list(cfg.commands.keys())


@app.post("/api/commands/{name}/run")
def commands_run(name: str, cwd: str):
    cfg = get_config()
    spec = cfg.commands.get(name)
    if not spec:
        raise HTTPException(status_code=404, detail="unknown command")
    try:
        result = run_whitelisted_command(spec, cwd)
    except CommandNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    history = CommandHistory(Path(cfg.logging.dir) / "command_history.jsonl")
    history.record(result)
    return {
        "run_id": result.run_id,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_seconds": result.duration_seconds,
        "timed_out": result.timed_out,
    }


@app.get("/api/commands/history")
def commands_history(limit: int = 50):
    cfg = get_config()
    history = CommandHistory(Path(cfg.logging.dir) / "command_history.jsonl")
    return history.recent(limit)


@app.get("/api/backup/snapshots")
def backup_snapshots_api():
    cfg = get_config()
    try:
        return backup_mod.list_snapshots(cfg.backup, cfg.disk)
    except backup_mod.BackupBlockedError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/backup/files")
def backup_files_api(snapshot_id: str, path: str = "/"):
    """Browse one restic snapshot without exposing the raw repository as a
    writable disk.  Uploads always belong in the live workspace instead."""
    cfg = get_config()
    try:
        return backup_mod.list_snapshot_directory(snapshot_id, path, cfg.backup, cfg.disk)
    except backup_mod.BackupBlockedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _dump_stream(process) -> Iterator[bytes]:
    """Yield an archived file in bounded chunks and never leave restic behind
    when a phone cancels its download."""
    try:
        assert process.stdout is not None
        while chunk := process.stdout.read(128 * 1024):
            yield chunk
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:
            if process.poll() is None:
                process.kill()


@app.get("/api/backup/files/download")
def backup_file_download_api(snapshot_id: str, path: str):
    cfg = get_config()
    try:
        entries = backup_mod.list_snapshot_directory(snapshot_id, str(Path(path).parent), cfg.backup, cfg.disk)
    except backup_mod.BackupBlockedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    target = next((entry for entry in entries if entry["path"] == path and not entry["is_dir"]), None)
    if not target:
        raise HTTPException(status_code=404, detail="archived file not found")
    try:
        process = backup_mod.open_snapshot_file(snapshot_id, path, cfg.backup, cfg.disk)
    except backup_mod.BackupBlockedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    filename = quote(Path(path).name)
    return StreamingResponse(
        _dump_stream(process), media_type=guess_mimetype(Path(path)),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}", "Cache-Control": "no-store"},
    )


def _run_dashboard_backup(source_path: str, cfg: Config) -> None:
    """Run the existing restic workflow off the request thread.

    ``run_backup`` owns the durable status file, including terminal failure
    state, so a browser can refresh `/api/backup/status` while a long backup is
    running.  Always release the in-process admission lock, even if an
    unexpected exception escapes the backup engine.
    """
    try:
        backup_mod.run_backup(
            source_path, cfg.backup, cfg.disk, tags=["dashboard"],
            status_path=backup_mod.backup_status_path(cfg.logging.dir),
        )
    finally:
        _dashboard_backup_lock.release()


@app.post("/api/backup/run", status_code=202)
def backup_run_api(background_tasks: BackgroundTasks):
    cfg = get_config()
    state = resolve_workspace_state(cfg)
    if not state.is_active:
        raise HTTPException(status_code=503, detail=f"Nothing to back up: {state.detail}")

    pi_status = backup_mod.read_backup_status(cfg.logging.dir)
    mac_status = backup_mod.read_backup_status_file(backup_mod.mac_backup_status_path(cfg.logging.dir))
    if pi_status.get("active") or mac_status.get("active"):
        raise HTTPException(status_code=409, detail="A backup is already active; wait for its status to become terminal before starting another one.")
    if not _dashboard_backup_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A backup start is already being processed.")

    background_tasks.add_task(_run_dashboard_backup, state.active_path, cfg)
    return {
        "accepted": True,
        "source": state.active_path,
        "status_url": "/api/backup/status",
    }


def _mac_bridge_http_request(cfg: Config, method: str, path: str, **kwargs):
    """Single choke point for every dashboard -> Mac bridge call. The Mac
    bridge already enforces its own allowlist (see mac_bridge/app.py) --
    this function only ever forwards to a fixed set of routes
    (/health, /capabilities, /files/*, /automations/*, /commands/{name}/run).
    It never accepts or
    forwards an arbitrary path/command from the caller beyond a command
    *name* the Mac bridge itself validates against its own allowlist. The
    Mac bridge token lives in the Pi's own environment (never sent to or
    accepted from the dashboard caller), so a dashboard caller can trigger
    only named, pre-approved Mac operations -- never arbitrary remote shell."""
    import httpx

    if not cfg.mac_bridge.enabled:
        raise HTTPException(status_code=503, detail="mac_bridge disabled in config")
    token = os.environ.get(cfg.mac_bridge.token_env_var)
    if not token:
        raise HTTPException(status_code=500, detail=f"{cfg.mac_bridge.token_env_var} is not set on the Pi")
    url = f"http://{cfg.mac_bridge.host}:{cfg.mac_bridge.port}{path}"
    timeout = kwargs.pop("_timeout", 15)
    try:
        r = httpx.request(method, url, headers={"X-Workspace-Token": token}, timeout=timeout, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Mac unreachable: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r


def _mac_bridge_request(cfg: Config, method: str, path: str, **kwargs):
    return _mac_bridge_http_request(cfg, method, path, **kwargs).json()


@app.get("/api/mac/status")
def mac_status_api():
    cfg = get_config()
    try:
        return _mac_bridge_request(cfg, "GET", "/health")
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/api/mac/capabilities")
def mac_capabilities_api():
    cfg = get_config()
    return _mac_bridge_request(cfg, "GET", "/capabilities")


@app.post("/api/mac/commands/{name}/run")
def mac_commands_run_api(name: str, cwd: str):
    cfg = get_config()
    # `name` and `cwd` are forwarded as-is to the Mac bridge, which re-checks
    # `name` against its own `cfg.commands` allowlist and `cwd` against that
    # command's own `allowed_roots` (see mac_bridge/app.py + commands.py) --
    # the dashboard performs no allowlist decision itself, it only routes.
    return _mac_bridge_request(cfg, "POST", f"/commands/{name}/run", params={"cwd": cwd})


@app.post("/api/mac/backup/run", status_code=202)
def mac_backup_run_api():
    cfg = get_config()
    mac_status = backup_mod.read_backup_status_file(backup_mod.mac_backup_status_path(cfg.logging.dir))
    if mac_status.get("active"):
        raise HTTPException(status_code=409, detail="A Mac backup is already active")
    return _mac_bridge_request(cfg, "POST", "/backup/run")


@app.get("/api/mac/files")
def mac_files_list(path: str):
    return _mac_bridge_request(get_config(), "GET", "/files", params={"path": path})


@app.get("/api/mac/files/content")
def mac_files_content(path: str):
    return _mac_bridge_request(get_config(), "GET", "/files/content", params={"path": path})


@app.get("/api/mac/files/download")
def mac_files_download(path: str):
    response = _mac_bridge_http_request(get_config(), "GET", "/files/download", params={"path": path})
    headers = {
        name: value for name, value in response.headers.items()
        if name.lower() in {"content-disposition", "cache-control"}
    }
    return Response(content=response.content,
                    media_type=response.headers.get("content-type", "application/octet-stream"),
                    headers=headers)


@app.post("/api/mac/files/upload")
async def mac_files_upload(dir_path: str, file: UploadFile):
    """Forward an upload to the Mac's narrow live-Workspace window.

    The request temporary file is sent directly to the authenticated bridge,
    avoiding a second in-memory copy on the Pi.
    """
    safe_name = Path(file.filename or "").name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid filename")
    try:
        response = _mac_bridge_http_request(
            get_config(), "POST", "/files/upload", params={"dir_path": dir_path},
            files={"file": (safe_name, file.file, file.content_type or "application/octet-stream")}, _timeout=600,
        )
        return response.json()
    finally:
        await file.close()


@app.post("/api/mac/files/folders", status_code=201)
def mac_files_create_folder(dir_path: str, name: str):
    return _mac_bridge_request(
        get_config(), "POST", "/files/folders", params={"dir_path": dir_path, "name": name},
    )


@app.get("/api/mac/automations")
def mac_automations_api():
    return _mac_bridge_request(get_config(), "GET", "/automations")


@app.post("/api/mac/automations/{automation_id}/run", status_code=202)
def mac_automation_run_api(automation_id: str):
    if not _AUTOMATION_ID_RE.fullmatch(automation_id):
        raise HTTPException(status_code=404, detail="unknown automation")
    return _mac_bridge_request(get_config(), "POST", f"/automations/{automation_id}/run")


def _automation_hub_request(method: str, path: str, **kwargs):
    """Call exactly one configured private Home Platform Control Plane.

    The browser cannot select a host or arbitrary route.  The registry stays
    the authority for which non-Mac automations exist and may be run.
    """
    import httpx

    cfg = get_config()
    if not cfg.automation_hub.enabled or not cfg.automation_hub.control_plane_url:
        raise HTTPException(status_code=503, detail="Home Platform automations are not configured")
    try:
        response = httpx.request(
            method, cfg.automation_hub.control_plane_url.rstrip("/") + path, timeout=15, **kwargs,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Home Platform unavailable: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.get("/api/automation-hub")
def automation_hub_api():
    cfg = get_config()
    if not cfg.automation_hub.enabled or not cfg.automation_hub.control_plane_url:
        return {"configured": False, "automations": [], "runs": []}
    registry = _automation_hub_request("GET", "/api/v1/registry")
    runs = _automation_hub_request("GET", "/api/v1/automations/runs")
    return {"configured": True, "automations": registry.get("automations", []), "runs": runs}


@app.post("/api/automation-hub/{automation_id}/run", status_code=202)
def automation_hub_run_api(automation_id: str):
    if not _AUTOMATION_ID_RE.fullmatch(automation_id):
        raise HTTPException(status_code=404, detail="unknown automation")
    return _automation_hub_request(
        "POST", f"/api/v1/automations/{automation_id}/runs",
        json={"trigger": "manual", "source": "dashboard"},
    )


@app.get("/api/platform/overview")
def platform_overview_api():
    """Home Platform's node/service/alert overview -- see
    docs/architecture/ecosystem-ownership.md: Home Platform owns this data,
    this route only ever proxies its already-authenticated loopback API."""
    cfg = get_config()
    if not cfg.automation_hub.enabled or not cfg.automation_hub.control_plane_url:
        return {"configured": False}
    overview = _automation_hub_request("GET", "/api/v1/platform/overview")
    return {"configured": True, **overview}


@app.get("/api/platform/alerts")
def platform_alerts_api():
    cfg = get_config()
    if not cfg.automation_hub.enabled or not cfg.automation_hub.control_plane_url:
        return {"configured": False, "alerts": []}
    alerts = _automation_hub_request("GET", "/api/v1/alerts")
    return {"configured": True, "alerts": alerts}


@app.post("/api/platform/devices/{device_id}/light-action")
def platform_light_action_api(device_id: str, payload: dict):
    """Forwards only to Home Platform's own light-action route, which in
    turn only ever calls a configured Home Assistant device action -- never
    a shell command. See home-platform's app/api.py::light_action."""
    if not _AUTOMATION_ID_RE.fullmatch(device_id):
        raise HTTPException(status_code=404, detail="unknown device")
    action = payload.get("action")
    if action not in {"toggle", "brightness"}:
        raise HTTPException(status_code=400, detail="action must be 'toggle' or 'brightness'")
    body = {"action": action}
    if action == "brightness":
        body["step"] = int(payload.get("step", 0))
    return _automation_hub_request("POST", f"/api/v1/devices/{device_id}/light-action", json=body)


# MARK: - Tapo power

@app.get("/api/power/status")
def power_status_api():
    return get_tapo_service().status()


@app.get("/api/power/history")
def power_history_api(device: str, hours: float = 24):
    return get_tapo_service().history(device, hours)


@app.get("/api/power/summary")
def power_summary_api(device: str, hours: float = 24):
    return get_tapo_service().summary(device, hours)


@app.post("/api/power/{device}/on")
async def power_on_api(device: str):
    try:
        reading = await get_tapo_service().set_power(device, True)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown Tapo device") from None
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"name": reading.device_name, "device_on": reading.device_on, "watts": reading.watts}


@app.post("/api/power/{device}/off")
async def power_off_api(device: str):
    try:
        reading = await get_tapo_service().set_power(device, False)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown Tapo device") from None
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"name": reading.device_name, "device_on": reading.device_on, "watts": reading.watts}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/kiosk", response_class=HTMLResponse)
def kiosk_index(request: Request):
    """Local-display entry point with an HttpOnly local-session cookie.

    This route only accepts loopback or the Pi's own dashboard bind address,
    which is how the Chromium kiosk reaches a Tailnet-bound dashboard.  The
    token is never injected into HTML or JavaScript; tailnet clients continue
    to use `/` and establish their own Secure HTTPS session.
    """
    host = request.client.host if request.client else ""
    local_hosts = {"127.0.0.1", "::1", resolve_bind_host(get_config().dashboard.bind_host)}
    if host not in local_hosts:
        raise HTTPException(status_code=404, detail="not found")
    token = os.environ.get(get_config().dashboard.auth_token_env_var)
    if not token:
        raise HTTPException(status_code=503, detail="dashboard token is not configured")
    response = HTMLResponse(INDEX_HTML)
    response.set_cookie(
        _SESSION_COOKIE_NAME,
        token,
        max_age=60 * 60 * 24 * 30,
        secure=False,  # this route is intentionally only the local HTTP kiosk.
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response
