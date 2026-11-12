"""The `/api/v1` gateway.

Public: `GET /healthz` (no detail). Pairing POSTs require an expiring invitation
secret. Other routes require a scoped bearer token, except media-session URLs,
whose unguessable path segment is a short-lived credential for one resource.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import PurePosixPath
from typing import Literal

from fastapi import Body, Depends, FastAPI, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from .. import CONTRACT_VERSION, SUPPORTED_CONTRACT_MAJORS, __version__
from ..articles import ArticleRelay, MAX_BODY
from ..opportunities import OpportunityRelay, MAX_BODY as MAX_OPPORTUNITY_BODY
from ..chewsense import ChewSenseMailbox
from ..connections import Connections
from ..auth import Principal
from ..cameras.legacy_media import CONTENT_TYPES
from ..errors import ContractVersionUnsupported, ServiceError, Unauthenticated
from ..jobs import KINDS
from ..legacy import build_backup_destination, build_backup_status, build_storage_contract
from ..runtime import iso
from ..services import Services

log = logging.getLogger("home_services.access")
_SESSION_PATH = re.compile(r"(/api/v1/media/sessions/)[^/?]+")
_TS_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.ts$")


# --- request models -----------------------------------------------------------

class DirectoryCreate(BaseModel):
    path: str = Field(..., description="Path relative to the files root")


class TrashCreate(BaseModel):
    path: str


class MoveRequest(BaseModel):
    from_path: str = Field(..., alias="from", description="Existing entry, relative to the files root")
    to: str = Field(..., description="New path, relative to the files root; its parent must exist")
    conflict: Literal["fail", "keep_both"] = "fail"


class UploadCreate(BaseModel):
    root: Literal["files"] = "files"
    path: str = Field(..., description="Destination file path relative to the root")
    size: int = Field(..., ge=0)
    sha256: str | None = Field(None, description="Lowercase hex SHA-256 of the whole file; verified at completion when supplied")
    conflict: Literal["keep_both", "fail"] = "keep_both"


class UploadComplete(BaseModel):
    create_parents: bool = False


class JobCreate(BaseModel):
    kind: str
    params: dict = Field(default_factory=dict)


class RestoreCreate(BaseModel):
    snapshot_id: str
    include: list[str] = Field(..., min_length=1, max_length=100, description="Absolute paths inside the snapshot")


class SourceRegister(BaseModel):
    host: str
    path: str
    tag: str
    scheduler: str = "files-backups-companion"
    repository: str = Field("primary", description="'primary' (the repository Home Services reads and verifies) or "
                                                   "'external:<label>' (status-only: runs are reported, not verified)")


class RunStart(BaseModel):
    scheduler: str = "files-backups-companion"


class RunHeartbeat(BaseModel):
    progress: dict | None = None


class RunFinish(BaseModel):
    state: Literal["succeeded", "failed", "cancelled"]
    snapshot_id: str | None = None
    summary: dict | None = None
    error: dict | None = None


class ArmRequest(BaseModel):
    armed: bool


class MediaSessionCreate(BaseModel):
    kind: Literal["legacy_event_media"]
    event_id: str
    name: str


class ProtectRequest(BaseModel):
    protected: bool


class TestNotification(BaseModel):
    message: str = "Home Services test notification"


# --- app factory --------------------------------------------------------------

class PairingPreview(BaseModel):
    model_config = {"extra": "forbid"}
    invitation_id: str = Field(min_length=1, max_length=80)
    secret: str = Field(min_length=16, max_length=128)


class PairingExchange(PairingPreview):
    client_secret: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    device_name: str = Field(min_length=1, max_length=120)


class ConnectionUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    scopes: list[str] | None = Field(default=None, max_length=64)
    preferences: dict | None = None

def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="Home Services", version=__version__,
                  description=f"Private Home Services API, contract {CONTRACT_VERSION}. See docs/API.md.",
                  openapi_url="/api/v1/openapi.json", docs_url=None, redoc_url=None)
    app.state.services = services
    S = services

    @app.middleware("http")
    async def contract_and_logging(request: Request, call_next):
        started = time.monotonic()
        requested = request.headers.get("home-services-contract")
        if requested is not None:
            try:
                major = int(requested.split(".")[0])
            except ValueError:
                major = -1
            if major not in SUPPORTED_CONTRACT_MAJORS:
                err = ContractVersionUnsupported(
                    f"this server implements contract {CONTRACT_VERSION}; update the client or server",
                    details={"requested": requested, "supported_majors": list(SUPPORTED_CONTRACT_MAJORS), "server_version": __version__})
                response = JSONResponse(err.as_dict(), status_code=err.status)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Home-Services-Contract"] = CONTRACT_VERSION
        if request.url.path in ("/api/v1/articles/review", "/api/v1/opportunities/relay") \
                or request.url.path.startswith(("/api/v1/chewsense/", "/api/v1/pairing/", "/api/v1/connection")):
            response.headers["Cache-Control"] = "no-store"
        path = _SESSION_PATH.sub(r"\1<redacted>", request.url.path)
        log.info("%s %s %s %.1fms", request.method, path, response.status_code, (time.monotonic() - started) * 1000)
        return response

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, exc: ServiceError):
        return JSONResponse(exc.as_dict(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        errs = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"error": {"code": "validation_failed", "message": "request validation failed", "details": {"errors": errs}}}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse({"error": {"code": code, "message": str(exc.detail), "details": {}}}, status_code=exc.status_code)

    def principal(authorization: str | None = Header(None)) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise Unauthenticated("missing bearer token")
        return S.tokens.authenticate(authorization[7:].strip())

    def scope(name: str):
        def dep(p: Principal = Depends(principal)) -> Principal:
            p.require(name)
            return p
        return dep

    def idem(idempotency_key: str | None = Header(None, alias="Idempotency-Key")) -> str | None:
        if idempotency_key is not None and not (1 <= len(idempotency_key) <= 200):
            raise ServiceError("Idempotency-Key must be 1-200 characters", code="invalid_idempotency_key")
        return idempotency_key

    def require_idem(key: str | None = Depends(idem)) -> str:
        if key is None:
            raise ServiceError("this operation requires an Idempotency-Key header", code="idempotency_key_required")
        return key

    from functools import lru_cache

    @lru_cache(maxsize=1)
    def connections():
        return Connections(S)

    @app.post("/api/v1/pairing/preview", tags=["connections"])
    def pairing_preview(body: PairingPreview):
        """Invitation secret in the body, never a query string or bearer token."""
        return connections().preview(body.invitation_id, body.secret)

    @app.post("/api/v1/pairing/exchange", tags=["connections"])
    def pairing_exchange(body: PairingExchange):
        return connections().exchange(body.invitation_id, body.secret,
                                      client_secret=body.client_secret, device_name=body.device_name)

    @app.get("/api/v1/connection", tags=["connections"])
    def own_connection(p: Principal = Depends(principal)):
        """Current server-enforced scopes and app-owned destination preferences."""
        return connections().for_token(p.token_id)

    @app.patch("/api/v1/connection", tags=["connections"])
    def update_own_connection(body: ConnectionUpdate, p: Principal = Depends(principal)):
        own = connections().for_token(p.token_id)
        return connections().update(own["id"], scopes=body.scopes, preferences=body.preferences)

    @app.delete("/api/v1/connection", tags=["connections"])
    def disconnect(p: Principal = Depends(principal)):
        return connections().revoke(connections().for_token(p.token_id)["id"])

    @lru_cache(maxsize=1)
    def article_relay():
        return ArticleRelay(S.config.data_dir / "article-relay.sqlite3", S.clock, S.server_id)

    @app.post("/api/v1/articles/review", tags=["articles"], description="Private durable article relay; never publishes. See docs/ARTICLE_SYNC.md for action shapes.")
    async def article_review(request: Request, p: Principal = Depends(principal)):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BODY:
                raise ServiceError("Article request exceeds 30 MB", status=413)
        try:
            body = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise ServiceError("Expected an article JSON request")
        return await run_in_threadpool(article_relay().handle, body, p)

    @lru_cache(maxsize=1)
    def opportunity_relay():
        return OpportunityRelay(S.config.data_dir / "opportunity-relay.sqlite3", S.clock, S.server_id)

    @app.post("/api/v1/opportunities/relay", tags=["opportunities"], description="Private durable Opportunity Discovery relay; never sends email. See docs/OPPORTUNITY_RELAY.md for action shapes.")
    async def opportunity_relay_route(request: Request, p: Principal = Depends(principal)):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_OPPORTUNITY_BODY:
                raise ServiceError("Opportunity request exceeds 25 MB", status=413)
        try:
            body = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise ServiceError("Expected an opportunity JSON request")
        return await run_in_threadpool(opportunity_relay().handle, body, p)

    # --- Chew Sense mailbox (docs/CHEWSENSE_MAILBOX.md) -------------------------

    @lru_cache(maxsize=1)
    def mailbox():
        return ChewSenseMailbox(S.config.data_dir / "chewsense-mailbox.sqlite3", S.storage, S.clock, S.ids)

    def any_scope(*names: str):
        def dep(p: Principal = Depends(principal)) -> Principal:
            if not p.scopes.intersection(names):
                p.require(names[0])
            return p
        return dep

    deposit_scope, collect_scope = scope("chewsense:deposit"), scope("chewsense:collect")
    either_scope = any_scope("chewsense:deposit", "chewsense:collect")

    @app.post("/api/v1/chewsense/deposits", status_code=201, tags=["chewsense"],
              description="Announce an encrypted recording: session_id, size, sha256 (sealed), archive_sha256, device.")
    def chewsense_create(body: dict = Body(...), p: Principal = Depends(deposit_scope)):
        view, created = mailbox().create(body, p.token_id)
        return JSONResponse(view, status_code=201 if created else 200)

    @app.get("/api/v1/chewsense/deposits/{deposit_id}", tags=["chewsense"])
    def chewsense_view(deposit_id: str, _p: Principal = Depends(either_scope)):
        return mailbox().view(deposit_id)

    @app.put("/api/v1/chewsense/deposits/{deposit_id}/content", tags=["chewsense"],
             openapi_extra={"requestBody": {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
    async def chewsense_append(deposit_id: str, request: Request, upload_offset: int = Header(..., alias="Upload-Offset", ge=0),
                               _p: Principal = Depends(deposit_scope)):
        limit = S.config.limits.max_chunk_bytes
        chunks, total = [], 0
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise ServiceError(f"chunk exceeds {limit} bytes", code="chunk_too_large", status=413)
                chunks.append(chunk)
        except ClientDisconnect:
            return JSONResponse({"error": {"code": "client_disconnected", "message": "request body incomplete", "details": {}}},
                                status_code=400)
        view = await run_in_threadpool(mailbox().append, deposit_id, upload_offset, chunks, limit)
        return JSONResponse(view, headers={"Upload-Offset": str(view["offset"])})

    @app.post("/api/v1/chewsense/deposits/{deposit_id}/complete", tags=["chewsense"])
    def chewsense_complete(deposit_id: str, _p: Principal = Depends(deposit_scope)):
        return mailbox().complete(deposit_id)

    @app.delete("/api/v1/chewsense/deposits/{deposit_id}", tags=["chewsense"])
    def chewsense_cancel(deposit_id: str, _p: Principal = Depends(deposit_scope)):
        return mailbox().cancel(deposit_id)

    @app.get("/api/v1/chewsense/pending", tags=["chewsense"])
    def chewsense_pending(_p: Principal = Depends(collect_scope)):
        return {"deposits": mailbox().pending()}

    @app.get("/api/v1/chewsense/deposits/{deposit_id}/content", tags=["chewsense"])
    def chewsense_download(deposit_id: str, _p: Principal = Depends(collect_scope)):
        return FileResponse(mailbox().blob(deposit_id), media_type="application/octet-stream",
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/chewsense/deposits/{deposit_id}/ack", tags=["chewsense"],
              description="result: imported | duplicate | rejected. The stored copy is deleted.")
    def chewsense_ack(deposit_id: str, body: dict = Body(...), _p: Principal = Depends(collect_scope)):
        return mailbox().acknowledge(deposit_id, body)

    @app.put("/api/v1/chewsense/status", tags=["chewsense"])
    def chewsense_put_status(body: dict = Body(...), _p: Principal = Depends(collect_scope)):
        return mailbox().put_status(body)

    @app.get("/api/v1/chewsense/status", tags=["chewsense"])
    def chewsense_get_status(_p: Principal = Depends(either_scope)):
        return mailbox().get_status()

    # --- public & bootstrap ---------------------------------------------------

    @app.get("/healthz", tags=["public"])
    def healthz():
        return {"status": "ok"}

    @app.get("/api/v1/bootstrap", tags=["bootstrap"])
    def bootstrap(p: Principal = Depends(principal)):
        return {**S.bootstrap(), "token": {"id": p.token_id, "name": p.name, "scopes": sorted(p.scopes)}}

    @app.get("/api/v1/capabilities", tags=["bootstrap"])
    def capabilities(_p: Principal = Depends(principal)):
        return {"server_id": S.server_id, "observed_at": iso(S.clock.now()), "capabilities": S.capabilities()}

    @app.get("/api/v1/readiness", tags=["bootstrap"])
    def readiness(_p: Principal = Depends(principal)):
        modules = S.module_readiness()
        return {"server_id": S.server_id, "observed_at": iso(S.clock.now()), "modules": modules}

    # --- storage ----------------------------------------------------------------

    @app.get("/api/v1/storage/volumes", tags=["storage"])
    def volumes(_p: Principal = Depends(scope("storage:read"))):
        out = []
        for v in S.config.volumes:
            st = S.guard.status(v).as_dict()
            st["roots"] = sorted(v.roots)
            out.append(st)
        return {"volumes": out}

    @app.get("/api/v1/storage/capacity", tags=["storage"])
    def capacity(_p: Principal = Depends(scope("storage:read"))):
        return S.storage.capacity()

    @app.get("/api/v1/storage/files/entries", tags=["storage"])
    def list_entries(path: str = "", limit: int = Query(200, ge=1, le=1000), cursor: str | None = None,
                     _p: Principal = Depends(scope("storage:read"))):
        return S.storage.list(path, limit=limit, cursor=cursor)

    @app.get("/api/v1/storage/files/entry", tags=["storage"])
    def get_entry(path: str, _p: Principal = Depends(scope("storage:read"))):
        return S.storage.entry(path)

    @app.get("/api/v1/storage/files/content", tags=["storage"])
    def download(path: str, _p: Principal = Depends(scope("storage:read"))):
        target = S.storage.file_for_download(path)
        return FileResponse(target, filename=target.name, content_disposition_type="attachment")

    @app.post("/api/v1/storage/files/directories", status_code=201, tags=["storage"])
    def mkdir(body: DirectoryCreate, _p: Principal = Depends(scope("storage:write"))):
        return S.storage.mkdir(body.path)

    @app.post("/api/v1/storage/files/move", tags=["storage"])
    def move(body: MoveRequest, _p: Principal = Depends(scope("storage:write"))):
        return S.storage.move(body.from_path, body.to, conflict=body.conflict)

    @app.post("/api/v1/storage/files/trash", status_code=201, tags=["storage"])
    def trash(body: TrashCreate, p: Principal = Depends(scope("storage:write"))):
        return S.storage.trash(body.path, p.token_id)

    @app.get("/api/v1/storage/files/trash", tags=["storage"])
    def list_trash(_p: Principal = Depends(scope("storage:read"))):
        return {"items": S.storage.list_trash(), "retention_days": S.config.limits.trash_retention_days}

    @app.post("/api/v1/storage/files/trash/{trash_id}/restore", tags=["storage"])
    def restore_trash(trash_id: str, _p: Principal = Depends(scope("storage:write"))):
        return S.storage.restore(trash_id)

    # --- transfers --------------------------------------------------------------

    @app.post("/api/v1/transfers/uploads", status_code=201, tags=["transfers"])
    def create_upload(body: UploadCreate, key: str | None = Depends(idem), p: Principal = Depends(scope("transfers:write"))):
        view, created = S.transfers.create(root=body.root, path=body.path, size=body.size, sha256=body.sha256,
                                           conflict=body.conflict, idempotency_key=key, principal_id=p.token_id)
        return JSONResponse(view, status_code=201 if created else 200)

    @app.get("/api/v1/transfers/uploads/{upload_id}", tags=["transfers"])
    def get_upload(upload_id: str, _p: Principal = Depends(scope("transfers:write"))):
        return S.transfers.view(upload_id)

    @app.put("/api/v1/transfers/uploads/{upload_id}/content", tags=["transfers"],
             openapi_extra={"requestBody": {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
    async def append_upload(upload_id: str, request: Request, upload_offset: int = Header(..., alias="Upload-Offset", ge=0),
                            _p: Principal = Depends(scope("transfers:write"))):
        limit = S.config.limits.max_chunk_bytes
        chunks, total = [], 0
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise ServiceError(f"chunk exceeds {limit} bytes", code="chunk_too_large", status=413)
                chunks.append(chunk)
        except ClientDisconnect:
            # Normal on phones (network change, suspension). Nothing was written; the client resumes from GET.
            log.info("upload %s: client disconnected after %d bytes of a chunk; acknowledged offset unchanged", upload_id, total)
            return JSONResponse({"error": {"code": "client_disconnected", "message": "request body incomplete", "details": {}}},
                                status_code=400)
        view = await run_in_threadpool(S.transfers.append, upload_id, upload_offset, chunks)
        return JSONResponse(view, headers={"Upload-Offset": str(view["offset"])})

    @app.post("/api/v1/transfers/uploads/{upload_id}/complete", tags=["transfers"])
    def complete_upload(upload_id: str, body: UploadComplete = Body(default_factory=UploadComplete),
                        _p: Principal = Depends(scope("transfers:write"))):
        return S.transfers.complete(upload_id, create_parents=body.create_parents)

    @app.delete("/api/v1/transfers/uploads/{upload_id}", tags=["transfers"])
    def cancel_upload(upload_id: str, _p: Principal = Depends(scope("transfers:write"))):
        return S.transfers.cancel(upload_id)

    # --- jobs -------------------------------------------------------------------

    @app.get("/api/v1/jobs", tags=["jobs"])
    def list_jobs(state: str | None = None, kind: str | None = None, limit: int = Query(50, ge=1, le=200),
                  _p: Principal = Depends(scope("jobs:read"))):
        return {"jobs": S.jobs.list(state=state, kind=kind, limit=limit)}

    @app.get("/api/v1/jobs/{job_id}", tags=["jobs"])
    def get_job(job_id: str, _p: Principal = Depends(scope("jobs:read"))):
        return S.jobs.get(job_id)

    @app.post("/api/v1/jobs", status_code=201, tags=["jobs"])
    def create_job(body: JobCreate, key: str = Depends(require_idem), p: Principal = Depends(scope("jobs:write"))):
        if body.kind not in KINDS or not KINDS[body.kind][1]:
            raise ServiceError(f"job kind {body.kind!r} cannot be created through this endpoint", code="unknown_job_kind",
                               details={"creatable_kinds": sorted(k for k, (_q, c) in KINDS.items() if c)})
        if body.params:
            raise ServiceError("storage.cleanup takes no parameters", code="invalid_params")
        job, created = S.jobs.create(body.kind, {}, idempotency_key=key, created_by=p.token_id)
        return JSONResponse(job, status_code=201 if created else 200)

    @app.post("/api/v1/jobs/{job_id}/cancel", tags=["jobs"])
    def cancel_job(job_id: str, _p: Principal = Depends(scope("jobs:write"))):
        return S.jobs.request_cancel(job_id)

    # --- backups ----------------------------------------------------------------

    @app.get("/api/v1/backups/repository", tags=["backups"])
    def repository(_p: Principal = Depends(scope("backups:read"))):
        result = {**S.restic.repository_status(), "writer_lease": S.backups.lease()}
        if len(S.recovery.readers) > 1:
            result["recovery_repositories"] = [{"id": name, **reader.repository_status()}
                                               for name, reader in S.recovery.readers.items()]
        return result

    @app.get("/api/v1/backups/snapshots", tags=["backups"])
    def snapshots(source_id: str | None = None, _p: Principal = Depends(scope("backups:read"))):
        snaps = S.recovery.snapshots()
        if source_id:
            source = S.backups.source(source_id)
            repository = "primary" if source["repository"] == "primary" else "companion"
            snaps = [s for s in snaps if source["tag"] in s["tags"] and source["path"] in s["paths"]
                     and s.get("repository", "primary") == repository]
        return {"snapshots": snaps}

    @app.get("/api/v1/backups/snapshots/{snapshot_id}/entries", tags=["backups"])
    def snapshot_entries(snapshot_id: str, path: str = "/", _p: Principal = Depends(scope("backups:read"))):
        return {"snapshot_id": snapshot_id, "path": path, "entries": S.recovery.list_dir(snapshot_id, path)}

    @app.get("/api/v1/backups/snapshots/{snapshot_id}/content", tags=["backups"])
    def snapshot_file(snapshot_id: str, path: str, _p: Principal = Depends(scope("backups:read"))):
        proc = S.recovery.open_dump(snapshot_id, path)

        def stream():
            try:
                assert proc.stdout is not None
                for block in iter(lambda: proc.stdout.read(256 * 1024), b""):
                    yield block
            finally:
                proc.stdout.close()
                proc.terminate()
                proc.wait()

        name = PurePosixPath(path).name.replace('"', "")
        return StreamingResponse(stream(), media_type="application/octet-stream",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/v1/backups/restores", status_code=201, tags=["backups"])
    def create_restore(body: RestoreCreate, key: str = Depends(require_idem), p: Principal = Depends(scope("backups:restore"))):
        from ..backups.restic import safe_snapshot_id, safe_snapshot_path
        params = {"snapshot_id": safe_snapshot_id(body.snapshot_id), "include": [safe_snapshot_path(i) for i in body.include]}
        reader, _ = S.recovery.resolve(params["snapshot_id"])
        if reader.repository_status()["state"] != "present":
            raise ServiceError("the repository is not available", code="repository_unavailable", status=503)
        job, created = S.jobs.create("backup.restore", params, idempotency_key=key, created_by=p.token_id)
        return JSONResponse(job, status_code=201 if created else 200)

    @app.get("/api/v1/backups/protection", tags=["backups"])
    def protection(_p: Principal = Depends(scope("backups:read"))):
        """Each snapshot series in the repository with the age of its newest
        snapshot. This is what is actually protected, regardless of reports."""
        return {"observed_at": iso(S.clock.now()), "overdue_after_seconds": S.backups.overdue_seconds, "groups": S.backups.groups()}

    @app.get("/api/v1/backups/sources", tags=["backups"])
    def sources(_p: Principal = Depends(scope("backups:read"))):
        return {"sources": S.backups.sources()}

    @app.post("/api/v1/backups/sources", status_code=201, tags=["backups"])
    def register_source(body: SourceRegister, p: Principal = Depends(scope("backups:report"))):
        src, created = S.backups.register_source(host=body.host, path=body.path, tag=body.tag, scheduler=body.scheduler,
                                                 principal_id=p.token_id, repository=body.repository)
        return JSONResponse(src, status_code=201 if created else 200)

    @app.get("/api/v1/backups/sources/{source_id}", tags=["backups"])
    def get_source(source_id: str, _p: Principal = Depends(scope("backups:read"))):
        S.backups.reap()
        return S.backups.source(source_id)

    @app.post("/api/v1/backups/sources/{source_id}/runs", status_code=201, tags=["backups"])
    def start_run(source_id: str, body: RunStart = Body(default_factory=RunStart), key: str = Depends(require_idem),
                  _p: Principal = Depends(scope("backups:report"))):
        run, created = S.backups.start_run(source_id, scheduler=body.scheduler, idempotency_key=key)
        payload = {**run, "repository": {"locator": S.config.backup.companion_repository or None,
                                         "lease_seconds": S.config.backup.lease_seconds}}
        return JSONResponse(payload, status_code=201 if created else 200)

    @app.post("/api/v1/backups/runs/{run_id}/heartbeat", tags=["backups"])
    def run_heartbeat(run_id: str, body: RunHeartbeat = Body(default_factory=RunHeartbeat), _p: Principal = Depends(scope("backups:report"))):
        return S.backups.heartbeat(run_id, body.progress)

    @app.post("/api/v1/backups/runs/{run_id}/finish", tags=["backups"])
    def run_finish(run_id: str, body: RunFinish, _p: Principal = Depends(scope("backups:report"))):
        return S.backups.finish(run_id, state=body.state, snapshot_id=body.snapshot_id, summary=body.summary, error=body.error)

    @app.get("/api/v1/backups/runs/{run_id}", tags=["backups"])
    def get_run(run_id: str, _p: Principal = Depends(scope("backups:read"))):
        S.backups.reap()
        return S.backups.run(run_id)

    # --- cameras & media --------------------------------------------------------

    @app.get("/api/v1/cameras", tags=["cameras"])
    def cameras(_p: Principal = Depends(scope("cameras:read"))):
        return {"engine": S.config.cameras.engine, "cameras": S.cameras.list()}

    @app.get("/api/v1/cameras/{camera_id}", tags=["cameras"])
    def camera(camera_id: str, _p: Principal = Depends(scope("cameras:read"))):
        return S.cameras.get(camera_id)

    @app.put("/api/v1/cameras/{camera_id}/armed", tags=["cameras"])
    def arm(camera_id: str, body: ArmRequest, _p: Principal = Depends(scope("cameras:control"))):
        return S.cameras.set_armed(camera_id, body.armed)

    @app.post("/api/v1/cameras/{camera_id}/live-sessions", status_code=201, tags=["cameras"])
    def live_session(camera_id: str, p: Principal = Depends(scope("cameras:control"))):
        """An HLS URL for the camera's live stream. The session stays valid
        while the player keeps fetching it (idle 2 min, at most 12 h)."""
        S.cameras.live_source(camera_id)
        secret, expires = S.media.create("live_hls", camera_id, p, ttl=S.media.LIVE_IDLE_SECONDS)
        return {"url": f"/api/v1/media/sessions/{secret}/index.m3u8", "protocol": "hls", "expires_at": iso(expires),
                "expiry": "sliding"}

    @app.get("/api/v1/cameras/{camera_id}/snapshot", tags=["cameras"],
             responses={200: {"content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}})
    async def snapshot(camera_id: str, width: int = Query(640, ge=64, le=1280), _p: Principal = Depends(scope("cameras:read"))):
        """JPEG of the newest live frame (for tiles and widgets). Not stored."""
        data = await run_in_threadpool(S.cameras.snapshot, camera_id, width)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})

    @app.get("/api/v1/cameras/{camera_id}/recordings", tags=["cameras"])
    def recordings(camera_id: str, limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
                   kind: Literal["motion", "continuous"] | None = None, _p: Principal = Depends(scope("cameras:read"))):
        S.cameras.device(camera_id)
        return S.camera_store.recordings(camera_id, limit=limit, cursor=cursor, kind=kind)

    @app.get("/api/v1/cameras/{camera_id}/recordings/{recording_id}", tags=["cameras"])
    def recording(camera_id: str, recording_id: str, _p: Principal = Depends(scope("cameras:read"))):
        S.cameras.device(camera_id)
        return S.camera_store.recording(camera_id, recording_id)

    @app.post("/api/v1/cameras/{camera_id}/recordings/{recording_id}/playback-sessions", status_code=201, tags=["cameras"])
    def playback_session(camera_id: str, recording_id: str, p: Principal = Depends(scope("cameras:read"))):
        S.cameras.device(camera_id)
        rec = S.camera_store.recording(camera_id, recording_id)
        if not rec["playable"]:
            raise ServiceError("no recorded video remains for this recording", code="recording_empty", status=404)
        secret, expires = S.media.create("recording_hls", f"{camera_id}/{recording_id}", p,
                                         ttl=max(S.config.media_session_seconds, 4 * 3600))
        return {"url": f"/api/v1/media/sessions/{secret}/index.m3u8", "protocol": "hls", "expires_at": iso(expires)}

    @app.post("/api/v1/cameras/{camera_id}/recordings/{recording_id}/exports", status_code=201, tags=["cameras"])
    async def export_recording(camera_id: str, recording_id: str, p: Principal = Depends(scope("cameras:read"))):
        S.cameras.device(camera_id)
        out = await run_in_threadpool(S.camera_store.export, camera_id, recording_id)
        secret, expires = S.media.create("export_file", out["name"], p, ttl=3600)
        url = f"/api/v1/media/sessions/{secret}"
        return {"download_url": url, "url": url, "filename": out["name"], "content_type": out["content_type"],
                "size_bytes": out["size_bytes"], "expires_at": iso(expires)}

    @app.delete("/api/v1/cameras/{camera_id}/recordings/{recording_id}", status_code=204, tags=["cameras"])
    def delete_recording(camera_id: str, recording_id: str, _p: Principal = Depends(scope("cameras:control"))):
        S.cameras.device(camera_id)
        result = S.camera_store.delete_recording(camera_id, recording_id)
        log.info("camera %s: recording %s deleted (%d chunks)", camera_id, recording_id, result["deleted_chunks"])
        return Response(status_code=204)

    @app.put("/api/v1/cameras/{camera_id}/recordings/{recording_id}/protected", tags=["cameras"])
    def protect_recording(camera_id: str, recording_id: str, body: ProtectRequest, _p: Principal = Depends(scope("cameras:control"))):
        S.cameras.device(camera_id)
        return S.camera_store.set_protected(camera_id, recording_id, body.protected)

    @app.get("/api/v1/media/legacy/events", tags=["media"])
    def legacy_events(limit: int = Query(50, ge=1, le=200), before: str | None = None, _p: Principal = Depends(scope("media:read"))):
        return {"events": S.legacy_media.events(limit=limit, before=before)}

    @app.post("/api/v1/media/sessions", status_code=201, tags=["media"])
    def media_session(body: MediaSessionCreate, p: Principal = Depends(scope("media:read"))):
        resource = f"{body.event_id}/{body.name}"
        S.legacy_media.media_path(resource)  # validates existence before issuing
        secret, expires = S.media.create(body.kind, resource, p)
        return {"url": f"/api/v1/media/sessions/{secret}", "expires_at": iso(expires), "kind": body.kind}

    @app.get("/api/v1/media/sessions/{secret}", tags=["media"])
    def media_content(secret: str):
        kind, resource = S.media.lookup(secret)
        if kind == "legacy_event_media":
            path = S.legacy_media.media_path(resource)
            return FileResponse(path, media_type=CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
                                headers={"Cache-Control": "private, no-store"})
        if kind == "export_file":
            path = S.camera_store.export_file(resource)
            return FileResponse(path, media_type="video/mp4", filename=path.name, headers={"Cache-Control": "private, no-store"})
        raise ServiceError("this session is a playlist; fetch index.m3u8 below it", code="not_found", status=404)

    @app.get("/api/v1/media/sessions/{secret}/{name}", tags=["media"])
    def media_stream(secret: str, name: str):
        """HLS playlists and segments for live and recording sessions."""
        kind, resource = S.media.lookup(secret)
        no_store = {"Cache-Control": "private, no-store"}
        if kind == "live_hls":
            dev = S.cameras.device(resource)
            source = PurePosixPath(dev.source)
            if name == "index.m3u8":
                try:
                    text = open(dev.source).read()
                except OSError:
                    raise ServiceError("the camera's live stream is not available", code="camera_offline", status=503) from None
                return Response(text, media_type="application/vnd.apple.mpegurl", headers=no_store)
            if not _TS_NAME.match(name):
                raise ServiceError("segment not found", code="not_found", status=404)
            path = f"{source.parent}/{name}"
            try:
                data = open(path, "rb").read()  # live segments rotate away quickly; read whole (≈350 KB)
            except OSError:
                raise ServiceError("segment no longer available", code="not_found", status=404) from None
            return Response(data, media_type="video/mp2t", headers=no_store)
        if kind == "recording_hls":
            camera_id, _, rid = resource.partition("/")
            if name == "index.m3u8":
                return Response(S.camera_store.playlist(camera_id, rid), media_type="application/vnd.apple.mpegurl", headers=no_store)
            return FileResponse(S.camera_store.chunk_file(camera_id, rid, name), media_type="video/mp2t", headers=no_store)
        raise ServiceError("media not found", code="not_found", status=404)

    # --- notifications ----------------------------------------------------------

    @app.get("/api/v1/notifications/outbox", tags=["notifications"])
    def outbox(state: str | None = None, _p: Principal = Depends(scope("notifications:admin"))):
        return {"summary": S.outbox.summary(), "messages": S.outbox.list(state=state)}

    @app.post("/api/v1/notifications/test", status_code=202, tags=["notifications"])
    def test_notification(body: TestNotification = Body(default_factory=TestNotification), key: str = Depends(require_idem),
                          _p: Principal = Depends(scope("notifications:admin"))):
        msg, _ = S.outbox.enqueue(f"test:{key}", "Home Services", body.message, tags=["test_tube"])
        return msg

    # --- legacy Home Platform compatibility -------------------------------------

    def legacy_principal(authorization: str | None = Header(None), x_workspace_token: str | None = Header(None)) -> Principal:
        """Accepts a token with the legacy:storage-contract scope as a bearer
        token or in the predecessor's `X-Workspace-Token` header."""
        presented = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else x_workspace_token
        p = S.tokens.authenticate(presented)
        p.require("legacy:storage-contract")
        if not S.config.legacy_contract.enabled:
            raise ServiceError("the legacy storage contract is disabled on this host", code="capability_unavailable", status=404)
        return p

    @app.get("/api/storage/contract", tags=["legacy"])
    def storage_contract(_p: Principal = Depends(legacy_principal)):
        """Predecessor-compatible storage contract for Home Platform."""
        return build_storage_contract(S.config, S.guard)

    @app.get("/api/backup/destination", tags=["legacy"])
    def backup_destination(_p: Principal = Depends(legacy_principal)):
        """Predecessor-compatible backup-disk view (no restic call)."""
        return build_backup_destination(S.config, S.guard)

    @app.get("/api/backup/status", tags=["legacy"])
    def backup_status(_p: Principal = Depends(legacy_principal)):
        """Predecessor-compatible backup summary derived from repository
        snapshots and Home Services run state."""
        return build_backup_status(S)

    return app
