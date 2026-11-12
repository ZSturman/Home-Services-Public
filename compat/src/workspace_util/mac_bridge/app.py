"""Mac companion service.

Runs on the Mac as a LaunchAgent (see services/mac/com.workspace.macbridge.plist),
bound to the Mac's Tailscale IP only. Same whitelist-only execution model as
the dashboard's command runner -- no generic shell endpoint here either.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from ..commands import CommandHistory, CommandNotAllowedError, run_whitelisted_command
from ..config import Config, load_config
from ..files import guess_mimetype, list_directory, read_text_preview
from ..paths import PathNotAllowedError, resolve_within_roots

app = FastAPI(title="Workspace Mac Bridge")
_config: Config | None = None
MAC_BACKUP_LABEL = "com.workspace.mac-restic-backup"
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_AUTOMATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _require_auth(request: Request, cfg: Config) -> None:
    expected = os.environ.get(cfg.mac_bridge.token_env_var)
    if not expected:
        raise HTTPException(status_code=500, detail=f"{cfg.mac_bridge.token_env_var} is not set on this Mac")
    provided = request.headers.get("X-Workspace-Token")
    if provided != expected:
        raise HTTPException(status_code=401, detail="missing or invalid X-Workspace-Token")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path != "/health":
        try:
            _require_auth(request, get_config())
        except HTTPException as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "hostname": platform.node()}


@app.get("/capabilities")
def capabilities():
    cfg = get_config()
    domain = f"gui/{os.getuid()}"
    job = subprocess.run(["launchctl", "print", f"{domain}/{MAC_BACKUP_LABEL}"],
                         capture_output=True, text=True)
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "commands": list(cfg.commands.keys()),
        "workspace_path": cfg.workspace.local_mac_path,
        "projects_path": str(Path(cfg.workspace.local_mac_path) / cfg.workspace.projects_subdir),
        "backup_available": job.returncode == 0 and "state = running" not in job.stdout,
        "backup_label": MAC_BACKUP_LABEL if job.returncode == 0 else None,
    }


def _workspace_path(cfg: Config, path: str) -> Path:
    """Resolve a requested file within the Mac's configured Workspace only.

    The Pi dashboard never gets a generic remote-filesystem route: this is a
    deliberately narrow read-only window into the path the user has configured
    as their live Workspace on the Mac.
    """
    try:
        return resolve_within_roots(path, [cfg.workspace.local_mac_path])
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _folder_name(name: str) -> str:
    value = str(name or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise HTTPException(status_code=400, detail="folder name must be one simple name")
    return value


def _hammerspoon_json(code: str) -> object:
    """Run a fixed expression in the already-running Hammerspoon process.

    The dashboard never supplies Lua.  Calls below interpolate only an ID that
    has passed ``_AUTOMATION_ID_RE`` and the Hammerspoon side exposes only
    explicit ``remoteAction`` entries, not its full menu or a shell bridge.
    """
    cfg = get_config()
    if not cfg.mac_bridge.automation_enabled:
        raise HTTPException(status_code=503, detail="Hammerspoon dashboard actions are disabled in config")
    try:
        result = subprocess.run(
            [cfg.mac_bridge.hammerspoon_cli, "-c", code],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail="Hammerspoon is not available on this Mac") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=503, detail=result.stderr.strip() or "Hammerspoon did not accept the request")
    try:
        import json
        return json.loads(result.stdout.strip())
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Hammerspoon returned an invalid dashboard response") from exc


@app.get("/files")
def files_list(path: str):
    resolved = _workspace_path(get_config(), path)
    try:
        entries = list_directory(resolved)
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="not a directory") from exc
    return [
        {"name": entry.name, "path": entry.path, "is_dir": entry.is_dir,
         "size_bytes": entry.size_bytes, "modified": entry.modified}
        for entry in entries
    ]


@app.get("/files/content")
def files_content(path: str):
    resolved = _workspace_path(get_config(), path)
    try:
        content = read_text_preview(resolved)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": content.path, "text": content.text, "truncated": content.truncated}


@app.get("/files/download")
def files_download(path: str):
    resolved = _workspace_path(get_config(), path)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(resolved, media_type=guess_mimetype(resolved), filename=resolved.name)


@app.post("/files/upload")
async def files_upload(dir_path: str, file: UploadFile):
    resolved_dir = _workspace_path(get_config(), dir_path)
    if not resolved_dir.is_dir():
        raise HTTPException(status_code=400, detail="target directory does not exist")
    safe_name = Path(file.filename or "").name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="invalid filename")
    destination = resolved_dir / safe_name
    try:
        with open(destination, "xb") as handle:
            while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                handle.write(chunk)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="a file with that name already exists") from exc
    finally:
        await file.close()
    return {"saved": str(destination)}


@app.post("/files/folders", status_code=201)
def files_create_folder(dir_path: str, name: str):
    resolved_dir = _workspace_path(get_config(), dir_path)
    if not resolved_dir.is_dir():
        raise HTTPException(status_code=400, detail="target directory does not exist")
    destination = resolved_dir / _folder_name(name)
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="a file or folder with that name already exists") from exc
    return {"created": str(destination)}


@app.get("/automations")
def automations_list():
    return _hammerspoon_json(
        'local m=require("modules.core.automation_menu"); print(hs.json.encode(m.dashboardSnapshot()))'
    )


@app.post("/automations/{automation_id}/run", status_code=202)
def automation_run(automation_id: str):
    if not _AUTOMATION_ID_RE.fullmatch(automation_id):
        raise HTTPException(status_code=404, detail="unknown automation")
    return _hammerspoon_json(
        'local m=require("modules.core.automation_menu"); print(hs.json.encode(m.runDashboardAction("'
        + automation_id + '")))'
    )


@app.post("/backup/run", status_code=202)
def backup_run():
    """Start the installed one-shot Mac restic LaunchAgent.

    The project already owns this job and its durable status file on the Pi.
    We deliberately invoke its fixed label rather than accepting a command,
    shell text, source path, repository, or credential from a dashboard user.
    """
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{MAC_BACKUP_LABEL}"
    job = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
    if job.returncode != 0:
        raise HTTPException(status_code=503, detail="Mac backup LaunchAgent is not installed")
    if "state = running" in job.stdout:
        raise HTTPException(status_code=409, detail="Mac backup is already running")
    started = subprocess.run(["launchctl", "kickstart", target], capture_output=True, text=True)
    if started.returncode != 0:
        detail = started.stderr.strip() or "launchctl could not start the Mac backup"
        raise HTTPException(status_code=503, detail=detail)
    return {"accepted": True, "job": MAC_BACKUP_LABEL, "status_url": "/api/backup/status"}


@app.post("/commands/{name}/run")
def run_command(name: str, cwd: str):
    cfg = get_config()
    spec = cfg.commands.get(name)
    if not spec:
        raise HTTPException(status_code=404, detail="unknown command")
    try:
        result = run_whitelisted_command(spec, cwd)
    except CommandNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    history = CommandHistory(Path(cfg.logging.dir) / "mac_command_history.jsonl")
    history.record(result)
    return {
        "run_id": result.run_id,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_seconds": result.duration_seconds,
        "timed_out": result.timed_out,
    }
