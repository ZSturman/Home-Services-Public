"""Mac sync for camera media, built on the project's existing Syncthing
integration (syncthing.py) rather than a second, parallel sync mechanism.

A dedicated "camera" Syncthing folder is send-only on the Pi and
receive-only on the Mac: the Pi stays authoritative (its copy is never
touched by remote changes), capture keeps working while the Mac is offline
(Syncthing just queues), and reconnect catches up automatically -- all
properties Syncthing already provides. This module only wires that folder up
and reads its status back for the dashboard; it never implements transfer,
retry, or integrity checking itself. See docs/camera.md#mac-sync.
"""
from __future__ import annotations

from .config import Config
from .syncthing import SyncthingClient

CAMERA_SYNCTHING_FOLDER_ID = "camera"


def setup_pi_side(client: SyncthingClient, mac_device_id: str, storage_dir: str) -> None:
    """Run once on the Pi (see docs/camera.md#mac-sync setup). Mirrors the
    existing docs/syncthing.md pairing pattern for the "workspace" folder."""
    client.upsert_folder(
        CAMERA_SYNCTHING_FOLDER_ID, "Pi Camera (send-only)", storage_dir,
        [mac_device_id], paused=True,
    )
    config = client.get_config()
    for folder in config["folders"]:
        if folder["id"] == CAMERA_SYNCTHING_FOLDER_ID:
            folder["type"] = "sendonly"
    client.replace_config(config)


def setup_mac_side(client: SyncthingClient, pi_device_id: str, mac_sync_path: str) -> None:
    """Run once on the Mac. receive-only: the Mac's copy can never be edited
    or deleted in a way that propagates back and disturbs the Pi's
    authoritative copy."""
    client.upsert_folder(
        CAMERA_SYNCTHING_FOLDER_ID, "Pi Camera (received)", mac_sync_path,
        [pi_device_id], paused=True,
    )
    config = client.get_config()
    for folder in config["folders"]:
        if folder["id"] == CAMERA_SYNCTHING_FOLDER_ID:
            folder["type"] = "receiveonly"
    client.replace_config(config)


def resume_if_storage_ready(client: SyncthingClient, storage_dir: str) -> bool:
    """Mount-gated resume, matching resume_workspace_folder_if_mounted's
    guard: only unpause once storage_dir is a real, already-populated
    directory (never resume onto a not-yet-mounted path)."""
    from pathlib import Path
    if not Path(storage_dir).is_dir():
        client.set_folder_paused(CAMERA_SYNCTHING_FOLDER_ID, True)
        return False
    client.set_folder_paused(CAMERA_SYNCTHING_FOLDER_ID, False)
    return True


def folder_sync_status(cfg: Config) -> dict:
    """Best-effort camera sync status for the dashboard: never raises --
    a Syncthing hiccup degrades to {"configured": False, ...} rather than
    taking the dashboard down. Fields:
      configured        -- the "camera" folder exists in local Syncthing config
      mac_reachable     -- the paired Mac device is currently connected
      pending_items     -- files not yet fully synced to the Mac
      last_seen_mac_at  -- last time the Mac device was connected (epoch seconds)
      errors            -- recent folder-level sync errors, if any
    """
    if not cfg.camera.sync_enabled:
        return {"configured": False, "reason": "camera.sync_enabled is false"}
    try:
        client = SyncthingClient.from_local_config()
        config = client.get_config()
        folder = next((f for f in config["folders"] if f["id"] == CAMERA_SYNCTHING_FOLDER_ID), None)
        if folder is None:
            return {"configured": False, "reason": "camera folder not set up in Syncthing yet"}
        device_ids = [d["deviceID"] for d in folder.get("devices", [])
                      if d["deviceID"] != client.my_device_id()]
        mac_device_id = device_ids[0] if device_ids else None
        connections = client.connections().get("connections", {})
        mac_conn = connections.get(mac_device_id, {}) if mac_device_id else {}
        status = client.folder_status(CAMERA_SYNCTHING_FOLDER_ID)
        # needFiles/needBytes on *this* (send-only) folder describe what the
        # LOCAL side needs, which stays 0 regardless of remote delivery state
        # -- see SyncthingClient.completion's docstring. What the dashboard
        # actually wants to show ("how much is still pending delivery to the
        # Mac") comes from asking Syncthing how complete the Mac specifically
        # is against this folder.
        need_files = 0
        need_bytes = 0
        if mac_device_id:
            try:
                completion = client.completion(CAMERA_SYNCTHING_FOLDER_ID, mac_device_id)
                need_files = completion.get("needItems", 0)
                need_bytes = completion.get("needBytes", 0)
            except Exception:
                pass  # best-effort -- fall back to 0 rather than raising
        return {
            "configured": True,
            "paused": folder.get("paused", False),
            "mac_reachable": bool(mac_conn.get("connected")),
            "last_seen_mac_at": mac_conn.get("at"),
            "need_files": need_files,
            "need_bytes": need_bytes,
            "global_files": status.get("globalFiles", 0),
            "errors": status.get("errors", 0),
            "state": status.get("state", "unknown"),
            "state_changed_at": status.get("stateChanged"),
        }
    except Exception as exc:  # noqa: BLE001 -- status must never raise
        return {"configured": False, "reason": f"Syncthing unavailable: {exc}"}
