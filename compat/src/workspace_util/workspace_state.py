"""Resolves which of three states the live workspace is actually in, so a
missing SSD/NVMe is a clearly-represented condition rather than a fatal
config error:

  PRODUCTION -- disk.workspace_uuid is set and that disk is actually mounted.
  TEST       -- no production disk configured, but workspace.test_workspace
                is enabled: a small, bounded directory on the Pi's own SD
                card stands in, solely so Syncthing can be exercised
                end-to-end before hardware exists. Never used for real data.
  PENDING    -- neither of the above. Nothing syncs. This is an expected,
                normal state, not an error -- see docs/architecture.md
                "workspace disk pending".

Switching from TEST/PENDING to PRODUCTION later is just: attach the disk,
run scripts/disk-setup.sh, set disk.workspace_uuid in config.yaml. This
resolver picks PRODUCTION over TEST automatically the moment that's true,
so no other code needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import Config
from .mount_safety import check_mount


class WorkspaceMode(str, Enum):
    PRODUCTION = "production"
    TEST = "test"
    PENDING = "pending"


@dataclass
class WorkspaceState:
    mode: WorkspaceMode
    active_path: str | None
    detail: str

    @property
    def is_active(self) -> bool:
        return self.mode is not WorkspaceMode.PENDING


class TestWorkspaceTooLargeError(RuntimeError):
    """The bounded SD-card test directory grew past its configured cap."""
    __test__ = False  # tell pytest this "Test*"-named class isn't a test case


def measure_test_workspace_bytes(path: str) -> int:
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def ensure_test_workspace_within_bounds(cfg: Config) -> int:
    """Raise TestWorkspaceTooLargeError if the bounded test directory has
    grown past its cap. Call this before enabling/resuming sync into it --
    it is the size-bound equivalent of mount_safety.require_mount."""
    tw = cfg.workspace.test_workspace
    size = measure_test_workspace_bytes(tw.path)
    if size > tw.max_bytes:
        raise TestWorkspaceTooLargeError(
            f"Test workspace at {tw.path} is {size} bytes, over its configured "
            f"{tw.max_bytes} byte cap. This directory lives on the Pi's SD card "
            "on purpose to stay small -- remove files (or raise the cap "
            "deliberately in config.yaml) before continuing."
        )
    return size


PRODUCTION_SYNCTHING_FOLDER_ID = "workspace"


@dataclass
class StorageContract:
    """The narrow, stable contract other projects (e.g. Bidirectional
    Project Hub / home-platform) are meant to consult instead of inspecting
    disks, UUIDs, /etc/fstab, or mount tables themselves. Exposed as
    `GET /api/storage/contract` on the dashboard and as `workspace storage
    contract` on the CLI -- both just serialize this. Adding a field here is
    the only supported way to give another project more storage information;
    they must never reach around it into this project's internals."""
    mode: str  # "production" | "test" | "pending"
    production_sync_safe: bool
    workspace_disk_mounted: bool
    workspace_root: str | None  # only set when mode == "production"
    syncthing_workspace_folder: str  # "paused" | "active" | "unknown"
    storage_health: str  # "ok" | "warning" | "error"
    storage_health_detail: str
    project_directory_creation_allowed: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "production_sync_safe": self.production_sync_safe,
            "workspace_disk_mounted": self.workspace_disk_mounted,
            "workspace_root": self.workspace_root,
            "syncthing_workspace_folder": self.syncthing_workspace_folder,
            "storage_health": self.storage_health,
            "storage_health_detail": self.storage_health_detail,
            "project_directory_creation_allowed": self.project_directory_creation_allowed,
            "detail": self.detail,
        }


def _syncthing_workspace_folder_state() -> str:
    try:
        from .syncthing import SyncthingClient
        client = SyncthingClient.from_local_config()
        config = client.get_config()
        folder = next((f for f in config.get("folders", []) if f["id"] == PRODUCTION_SYNCTHING_FOLDER_ID), None)
        if folder is None:
            return "unknown"
        return "paused" if folder.get("paused", True) else "active"
    except Exception:
        return "unknown"


def build_storage_contract(cfg: Config) -> StorageContract:
    """Build the one object another project should ever need to know
    "can I write into the real workspace right now, and if not, why not."
    Never raises -- every sub-check is best-effort so a Syncthing hiccup or a
    transient mount blip degrades a field to "unknown"/False rather than
    taking down whoever is asking (see docs on the storage-health contract)."""
    state = resolve_workspace_state(cfg)
    mode = state.mode.value

    workspace_disk_mounted = False
    if cfg.disk.workspace_uuid:
        try:
            workspace_disk_mounted = check_mount(cfg.disk.workspace_mount, required_uuid=cfg.disk.workspace_uuid).mounted
        except Exception:
            workspace_disk_mounted = False

    production_sync_safe = state.mode is WorkspaceMode.PRODUCTION and workspace_disk_mounted

    storage_health = "ok"
    storage_health_detail = state.detail
    if state.mode is WorkspaceMode.PENDING and cfg.disk.workspace_uuid:
        # A UUID that WAS configured but is now missing is a regression, not
        # the normal "no disk yet" state -- see doctor.py's identical logic.
        storage_health = "error"
    elif state.mode is not WorkspaceMode.PRODUCTION:
        storage_health = "warning"

    try:
        backup_status = check_mount(cfg.disk.backup_mount, required_uuid=cfg.disk.backup_uuid or None)
        if not backup_status.mounted and storage_health == "ok":
            storage_health = "error"
            storage_health_detail = f"backup HDD not mounted: {backup_status.reason}"
    except Exception:
        pass

    return StorageContract(
        mode=mode,
        production_sync_safe=production_sync_safe,
        workspace_disk_mounted=workspace_disk_mounted,
        workspace_root=state.active_path if state.mode is WorkspaceMode.PRODUCTION else None,
        syncthing_workspace_folder=_syncthing_workspace_folder_state(),
        storage_health=storage_health,
        storage_health_detail=storage_health_detail,
        # Deliberately identical to production_sync_safe today: a physical
        # project directory must never be created anywhere but the real,
        # mounted production disk. A future Pi-resident workspace node must
        # gate on this field before running project.create_directory rather
        # than falling back to the bounded SD-card test path.
        project_directory_creation_allowed=production_sync_safe,
        detail=state.detail,
    )


def resolve_workspace_state(cfg: Config) -> WorkspaceState:
    if cfg.disk.workspace_uuid:
        status = check_mount(cfg.disk.workspace_mount, required_uuid=cfg.disk.workspace_uuid)
        if status.mounted:
            return WorkspaceState(
                WorkspaceMode.PRODUCTION, cfg.workspace.pi_path,
                f"workspace SSD/NVMe mounted at {cfg.disk.workspace_mount}",
            )
        return WorkspaceState(
            WorkspaceMode.PENDING, None,
            f"workspace disk is configured (UUID {cfg.disk.workspace_uuid}) but not "
            f"currently mounted: {status.reason}. Sync into {cfg.disk.workspace_mount} is disabled.",
        )

    if cfg.workspace.test_workspace.enabled:
        tw = cfg.workspace.test_workspace
        Path(tw.path).mkdir(parents=True, exist_ok=True)
        try:
            size = ensure_test_workspace_within_bounds(cfg)
        except TestWorkspaceTooLargeError as exc:
            return WorkspaceState(WorkspaceMode.PENDING, None, str(exc))
        return WorkspaceState(
            WorkspaceMode.TEST, tw.path,
            f"TEST MODE -- bounded directory on the Pi's own system storage "
            f"({size}/{tw.max_bytes} bytes), for Syncthing end-to-end testing only. "
            "Not a substitute for the real workspace disk.",
        )

    return WorkspaceState(
        WorkspaceMode.PENDING, None,
        "No workspace SSD/NVMe is configured and test mode is disabled. "
        "Workspace synchronization is pending that hardware -- see docs/architecture.md.",
    )
