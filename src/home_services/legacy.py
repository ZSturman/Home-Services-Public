"""Compatibility implementation of the predecessor's `/api/storage/contract`.

Home Platform consumes this response shape today. The fields and their
safety semantics match the predecessor's `workspace_state.StorageContract`:
`production_sync_safe` and `project_directory_creation_allowed` are true only
when the configured workspace volume is mounted with its UUID.

Differences, by design: Home Services never creates the predecessor's
SD-card TEST workspace, so `mode` is only "production" or "pending" (a
consumer must already treat "test" and "pending" as unsafe), and it does not
manage Syncthing, so `syncthing_workspace_folder` is "unknown".
"""
from __future__ import annotations

from .config import Config
from .runtime import iso
from .volumes import VolumeGuard

FIELDS = ("mode", "production_sync_safe", "workspace_disk_mounted", "workspace_root", "syncthing_workspace_folder",
          "storage_health", "storage_health_detail", "project_directory_creation_allowed", "detail")


def build_storage_contract(config: Config, guard: VolumeGuard) -> dict:
    lc = config.legacy_contract
    workspace_subdir = lc.workspace_subdir
    workspace_mounted = False
    if lc.workspace_volume:
        volume = config.volume(lc.workspace_volume)
        status = guard.status(volume)
        workspace_mounted = status.verified
        if workspace_mounted:
            mode, detail = "production", f"workspace volume mounted at {volume.mount}"
            root = f"{volume.mount.rstrip('/')}/{workspace_subdir}"
        else:
            mode = "pending"
            detail = (f"workspace volume is configured but not currently verified: {status.reason}. "
                      f"Sync into {volume.mount} is disabled.")
            root = None
    else:
        mode, root = "pending", None
        detail = "No workspace volume is configured. Workspace synchronization is pending that hardware."

    health, health_detail = "ok", detail
    if mode == "pending" and lc.workspace_volume:
        health = "error"
    elif mode != "production":
        health = "warning"
    if lc.backup_volume:
        backup_status = guard.status(config.volume(lc.backup_volume))
        if not backup_status.verified and health == "ok":
            health, health_detail = "error", f"backup HDD not mounted: {backup_status.reason}"

    safe = mode == "production" and workspace_mounted
    return {
        "mode": mode, "production_sync_safe": safe, "workspace_disk_mounted": workspace_mounted, "workspace_root": root,
        "syncthing_workspace_folder": "unknown", "storage_health": health, "storage_health_detail": health_detail,
        "project_directory_creation_allowed": safe, "detail": detail,
    }


def build_backup_destination(config: Config, guard: VolumeGuard) -> dict:
    """The predecessor's `/api/backup/destination` shape. Never runs restic."""
    volume = config.volume_for_role("restic_repository")
    if volume is None:
        return {"state": "unavailable", "device": None, "uuid": None, "mount": None, "filesystem": None,
                "repository": None, "usage": None, "repository_state": "not_configured"}
    st = guard.status(volume)
    entry = guard.probe.entry_for(volume.mount) if st.verified else None
    usage = None
    if st.verified and st.total_bytes is not None:
        usage = {"total_bytes": st.total_bytes, "used_bytes": st.total_bytes - (st.free_bytes or 0), "available_bytes": st.free_bytes}
    return {"state": "mounted" if st.verified else "unavailable", "device": entry.device if entry else None,
            "uuid": volume.uuid, "mount": volume.mount, "filesystem": st.fstype,
            "repository": f"{volume.mount.rstrip('/')}/{volume.roots['restic_repository']}", "usage": usage,
            "repository_state": "not_checked"}


def _days(seconds: float) -> str:
    days = seconds / 86400
    return f"{days:.0f} days" if days >= 2 else f"{seconds / 3600:.0f} hours"


def build_backup_status(services) -> dict:
    """The fields Home Platform reads from the predecessor's `/api/backup/status`
    (`state`, `warning`, `error`), derived from repository snapshots rather than
    from command exit codes. States: running, completed, failed, unknown."""
    from .errors import ServiceError

    s = services
    base = {"source": "home-services", "observed_at": iso(s.clock.now())}
    lease = s.backups.lease()
    try:
        groups = s.backups.groups()
    except ServiceError as exc:
        return {**base, "state": "unknown", "active": lease is not None, "warning": None,
                "error": f"backup repository unreadable: {exc.message}"}
    if not groups:
        return {**base, "state": "unknown", "active": lease is not None, "warning": None, "error": "the repository holds no snapshots"}
    newest = max(groups, key=lambda g: -g["protection"]["age_seconds"])
    retired = [g for g in groups if g.get("retired")]
    groups = [g for g in groups if not g.get("retired")]
    overdue = [g for g in groups if g["protection"]["state"] == "overdue" and g["source_id"] is not None] or \
              [g for g in groups if g["protection"]["state"] == "overdue" and not any(
                  o["hostname"] == g["hostname"] and o["protection"]["state"] == "current" for o in groups)]
    notes = []
    if overdue:
        notes.append("Overdue: " + "; ".join(
            f"{g['hostname']} ({','.join(g['tags']) or 'untagged'}) last backed up {_days(g['protection']['age_seconds'])} ago" for g in overdue))
    empty = [g for g in groups if g["protection"].get("empty")]
    if empty:
        notes.append("Empty: " + "; ".join(f"{g['hostname']} ({','.join(g['tags']) or 'untagged'}) snapshots contain no files" for g in empty))
    warning = " ".join(notes) or None
    last_run = s.db.one("SELECT state, error, finished_at FROM backup_runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1")
    state, error = ("running" if lease else "completed"), None
    if not lease and last_run is not None and last_run["state"] in ("failed", "interrupted"):
        state, error = "failed", f"the most recent reported backup run {last_run['state']}"
    p = newest["protection"]
    return {**base, "state": state, "active": lease is not None, "warning": warning, "error": error,
            "last_success_at": p["last_success_at"], "snapshot_id": p["snapshot_id"], "hostname": newest["hostname"],
            "series": len(groups) + len(retired)}
