"""Wires components for one process (gateway or a worker)."""
from __future__ import annotations

from dataclasses import dataclass

from . import CONTRACT_VERSION, SUPPORTED_CONTRACT_MAJORS, __version__
from .auth import MediaSessions, TokenStore
from .backups.coordination import BackupCoordinator
from .backups.restic import Restic
from .backups.recovery import RecoveryCatalog
from .cameras.legacy_media import LegacyMedia
from .cameras.registry import CameraRegistry
from .cameras.store import CameraStore
from .config import ROLES, WRITABLE_ROLES, Config
from .db import Database
from .errors import ServiceError
from .jobs import JobStore
from .notifications import Outbox
from .runtime import Clock, IdFactory
from .storage import Storage
from .transfers import Transfers
from .volumes import MountProbe, VolumeGuard


@dataclass
class Services:
    config: Config
    db: Database
    clock: Clock
    ids: IdFactory
    guard: VolumeGuard
    tokens: TokenStore
    media: MediaSessions
    jobs: JobStore
    storage: Storage
    transfers: Transfers
    restic: Restic
    recovery: RecoveryCatalog
    backups: BackupCoordinator
    cameras: CameraRegistry
    camera_store: CameraStore
    legacy_media: LegacyMedia
    outbox: Outbox

    @property
    def server_id(self) -> str:
        sid = self.db.get_meta("server_id")
        if sid is None:
            sid = self.ids.new("srv")
            # Concurrent first requests/processes must advertise one identity.
            # INSERT OR IGNORE also works when called inside an existing tx.
            self.db.execute("INSERT OR IGNORE INTO meta (key,value) VALUES ('server_id',?)", (sid,))
            sid = self.db.get_meta("server_id")
        return sid

    def module_readiness(self) -> dict:
        """Per-module readiness. A failed module never hides the others."""
        modules = {}
        for v in self.config.volumes:
            st = self.guard.status(v)
            modules[f"volume:{v.id}"] = {"state": "ready" if st.verified else "unavailable", "reason": st.reason, "volume_state": st.state}
        for role in ("files", "staging"):
            modules[f"storage:{role}"] = self._root_ready(role)
        if self.config.backup.enabled:
            rs = self.restic.repository_status()
            if rs["state"] != "present":
                modules["backups"] = {"state": "unavailable", "reason": rs["reason"]}
            elif not rs["password_configured"]:
                modules["backups"] = {"state": "unavailable", "reason": "repository password not configured"}
            else:
                modules["backups"] = {"state": "ready", "reason": "repository present"}
            for name, reader in self.recovery.readers.items():
                if name == "primary":
                    continue
                status = reader.repository_status()
                modules[f"backups:{name}"] = {
                    "state": "ready" if status["state"] == "present" and status["password_configured"] else "unavailable",
                    "reason": status["reason"] if status.get("password_configured") else "repository password not configured",
                }
        else:
            modules["backups"] = {"state": "disabled", "reason": "backup.enabled is false"}
        if self.cameras.engine_active:
            states = {d.id: self.cameras.status(d.id)["state"] for d in self.config.cameras.devices}
            ready = all(v == "recording" for v in states.values())
            modules["cameras"] = {"state": "ready" if ready else "degraded", "engine": self.config.cameras.engine, "cameras": states}
        else:
            modules["cameras"] = {"state": "disabled", "reason": "engine_not_configured"}
        modules["notifications"] = {"state": "configured" if self.config.notifications.enabled else "disabled", **self.outbox.summary()}
        ok, fstype = self.guard.data_dir_filesystem_ok()
        modules["state_store"] = {"state": "ready" if ok else "unsafe", "filesystem": fstype}
        return modules

    def _root_ready(self, role: str) -> dict:
        try:
            self.guard.root_path(role, write=role in WRITABLE_ROLES)
            return {"state": "ready", "reason": "verified"}
        except ServiceError as exc:
            return {"state": "unavailable", "reason": exc.message, **exc.details}

    def capabilities(self) -> dict:
        """What this host can do right now. Unsupported features are listed
        with a reason rather than omitted or faked."""
        def cap(available: bool, reason: str = "", **extra) -> dict:
            d = {"state": "available" if available else "unavailable", **extra}
            if not available:
                d["reason"] = reason
            return d

        files = self._root_ready("files")["state"] == "ready"
        staging = self._root_ready("staging")["state"] == "ready"
        rs = self.restic.repository_status() if self.config.backup.enabled else {"state": "disabled", "password_configured": False}
        repo = rs["state"] == "present" and rs["password_configured"]
        primary_repo = repo
        lim = self.config.limits
        storage_reason = "files/staging root unavailable (see readiness)"
        backup_reason = "backups disabled" if not self.config.backup.enabled else f"repository {rs['state']}" if rs["state"] != "present" else "repository password not configured"
        for name, reader in self.recovery.readers.items():
            if name != "primary":
                status = reader.repository_status()
                if status["state"] != "present" or not status.get("password_configured"):
                    repo = False
                    backup_reason = f"{name} recovery repository unavailable (see readiness)"
        return {
            "connections": {"pairing": cap(True, protocol=1, invitation_ttl_seconds=300,
                                             methods=["qr", "paste"], per_device_credentials=True)},
            "articles": {"review_sync": cap(True, protocol=1, canonical_writer="mac-companion", publishes=False, snapshot_quota_bytes=512_000_000)},
            "opportunities": {"review_relay": cap(True, protocol=1, canonical_writer="mac-companion", sends_email=False, resource_quota_bytes=128_000_000)},
            "chewsense": {"mailbox": cap(staging, storage_reason, protocol=1, encrypted_by_client=True, held_quota_bytes=20 * 1024**3,
                                         max_deposit_bytes=4 * 1024**3, deleted_after_ack=True)},
            "storage": {
                "browse": cap(files, storage_reason),
                "download": cap(files, storage_reason, range_requests=True),
                "create_directory": cap(files, storage_reason),
                "move": cap(files, storage_reason, conflict_policies=["fail", "keep_both"]),
                "managed_trash": cap(files and staging, storage_reason, retention_days=lim.trash_retention_days),
                "permanent_delete": cap(False, "not offered; managed Trash purges after the retention window"),
                "capacity": cap(files, storage_reason),
                "volume_identity_checks": cap(True, uuid_verified=True),
            },
            "transfers": {
                "resumable_upload": cap(files and staging, storage_reason, max_chunk_bytes=lim.max_chunk_bytes,
                                        verification=["size", "sha256"], conflict_policies=["keep_both", "fail"],
                                        abandon_after_hours=lim.staging_abandon_hours, staging_cap_bytes=lim.staging_cap_bytes),
            },
            "backups": {
                "repository_status": cap(self.config.backup.enabled, "backups disabled"),
                "snapshots": cap(repo, backup_reason),
                "archived_file_download": cap(repo, backup_reason),
                "restore_jobs": cap(repo and files, backup_reason if not repo else storage_reason, destination="fresh directory under files/" + self.config.backup.restores_dir),
                "source_registration": cap(primary_repo, backup_reason, companion_repository=self.config.backup.companion_repository or None),
                "retention_prune": cap(False, "not owned by Home Services during migration"),
                "integrity_check": cap(False, "not owned by Home Services during migration"),
                "repository_initialization": cap(False, "never performed by Home Services"),
            },
            "cameras": {"engine": self.config.cameras.engine,
                        "per_camera": {d.id: self.cameras.capabilities(d.id) for d in self.config.cameras.devices}},
            "media": {"legacy_events": cap(self._root_ready("legacy_media")["state"] == "ready", "legacy media root unavailable", read_only=True),
                      "media_sessions": cap(True, ttl_seconds=self.config.media_session_seconds)},
            "notifications": {"ntfy_outbox": cap(self.config.notifications.enabled, "notifications disabled")},
            "legacy": {"storage_contract": cap(self.config.legacy_contract.enabled, "legacy_contract.enabled is false")},
        }

    def bootstrap(self) -> dict:
        return {
            "server_id": self.server_id, "product": "home-services", "version": __version__,
            "contract": {"version": CONTRACT_VERSION, "supported_majors": list(SUPPORTED_CONTRACT_MAJORS), "header": "Home-Services-Contract"},
            "environment": self.config.environment,
            "roots": [{"role": r, "volume_id": (v.id if (v := self.config.volume_for_role(r)) else None), "writable": r in WRITABLE_ROLES}
                      for r in ROLES if r != "companion_recovery" or self.config.volume_for_role(r)],
            "cameras": [{"id": d.id, "name": d.name} for d in self.config.cameras.devices],
        }


def build_services(config: Config, *, clock: Clock | None = None, ids: IdFactory | None = None, probe: MountProbe | None = None) -> Services:
    clock = clock or Clock()
    ids = ids or IdFactory()
    guard = VolumeGuard(config, probe)
    ok, fstype = guard.data_dir_filesystem_ok()
    if not ok:
        raise ServiceError(f"data_dir is on {fstype}; the state store must live on the application-data filesystem, not removable media",
                           code="unsafe_state_store")
    db = Database(config.data_dir / "home-services.sqlite3")
    storage = Storage(config, db, guard, clock, ids)
    restic = Restic(config, guard)
    recovery = RecoveryCatalog(restic)
    return Services(
        config=config, db=db, clock=clock, ids=ids, guard=guard,
        tokens=TokenStore(db, clock, ids), media=MediaSessions(db, clock, ids, config.media_session_seconds),
        jobs=JobStore(db, clock, ids), storage=storage, transfers=Transfers(config, db, storage, clock, ids),
        restic=restic, recovery=recovery, backups=BackupCoordinator(config, db, restic, clock, ids, recovery=recovery),
        cameras=CameraRegistry(config, db, guard, clock), camera_store=CameraStore(config, db, guard, clock, ids), legacy_media=LegacyMedia(guard), outbox=Outbox(config, db, clock, ids),
    )
