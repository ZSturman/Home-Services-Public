"""Syncthing integration.

We drive Syncthing entirely through its local REST API (never expose that
API over Tailscale -- see docs/security.md) rather than hand-editing
config.xml, so re-running setup is idempotent and safe. This module never
implements sync itself; it only configures/queries the real Syncthing daemon,
keeping "sync" (Syncthing's job) cleanly separate from "backup" (restic's
job) per design principle #2.
"""
from __future__ import annotations

import platform
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import httpx

from .mount_safety import MountSafetyError, require_mount


class SyncthingError(RuntimeError):
    pass


def config_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Syncthing"
    if system == "Linux":
        return Path.home() / ".local" / "state" / "syncthing"
    raise NotImplementedError(f"unsupported platform {system}")


def read_api_credentials(config_path: Path | None = None) -> tuple[str, int, str]:
    """Returns (api_key, gui_port, gui_address) parsed from Syncthing's own
    config.xml. Syncthing generates this on first launch."""
    path = config_path or (config_dir() / "config.xml")
    if not path.exists():
        raise SyncthingError(
            f"Syncthing config not found at {path}. Start Syncthing once first "
            "(it generates its config on first run) -- see docs/syncthing.md."
        )
    tree = ET.parse(path)
    gui = tree.getroot().find("gui")
    if gui is None:
        raise SyncthingError("no <gui> section in Syncthing config.xml")
    api_key = gui.findtext("apikey", default="")
    address = gui.findtext("address", default="127.0.0.1:8384")
    host, _, port = address.rpartition(":")
    if not api_key:
        raise SyncthingError("Syncthing config.xml has no API key yet")
    return api_key, int(port or 8384), host or "127.0.0.1"


@dataclass
class SyncthingClient:
    api_key: str
    port: int = 8384
    host: str = "127.0.0.1"

    @classmethod
    def from_local_config(cls) -> "SyncthingClient":
        api_key, port, host = read_api_credentials()
        return cls(api_key=api_key, port=port, host=host or "127.0.0.1")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key}

    def _get(self, path: str) -> dict | list:
        r = httpx.get(f"{self.base_url}{path}", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json_body) -> dict:
        r = httpx.post(f"{self.base_url}{path}", headers=self._headers(), json=json_body, timeout=10)
        r.raise_for_status()
        return r.json() if r.text else {}

    def ping(self) -> bool:
        try:
            self._get("/rest/system/ping")
            return True
        except Exception:
            return False

    def my_device_id(self) -> str:
        status = self._get("/rest/system/status")
        return status["myID"]

    def get_config(self) -> dict:
        return self._get("/rest/config")

    def replace_config(self, config: dict) -> None:
        r = httpx.put(f"{self.base_url}/rest/config", headers=self._headers(), json=config, timeout=15)
        r.raise_for_status()

    def add_device(self, device_id: str, name: str, addresses: list[str] | None = None) -> None:
        """`addresses` defaults to ["dynamic"] (LAN multicast + global relay
        discovery). Pass an explicit tcp://<tailscale-ip>:22000 to pin the
        connection to the tailnet only, per this project's "Tailscale
        provides connectivity" principle -- see docs/tailscale.md."""
        config = self.get_config()
        existing = next((d for d in config["devices"] if d["deviceID"] == device_id), None)
        if existing:
            if addresses:
                existing["addresses"] = addresses
                self.replace_config(config)
            return
        config["devices"].append({
            "deviceID": device_id,
            "name": name,
            "addresses": addresses or ["dynamic"],
            "introducer": False,
        })
        self.replace_config(config)

    def upsert_folder(
        self,
        folder_id: str,
        label: str,
        path: str,
        device_ids: list[str],
        paused: bool = True,
        ignore_perms: bool = False,
    ) -> None:
        """Create or update a folder definition. `paused=True` by default --
        callers must explicitly resume a folder only after confirming its
        underlying mount is present (see mount_safety / doctor)."""
        config = self.get_config()
        existing = next((f for f in config["folders"] if f["id"] == folder_id), None)
        entry = {
            "id": folder_id,
            "label": label,
            "path": path,
            "type": "sendreceive",
            "devices": [{"deviceID": d} for d in device_ids],
            "paused": paused,
            "ignorePerms": ignore_perms,
            "fsWatcherEnabled": True,
        }
        if existing:
            existing.update(entry)
        else:
            config["folders"].append(entry)
        self.replace_config(config)

    def set_folder_paused(self, folder_id: str, paused: bool) -> None:
        config = self.get_config()
        for folder in config["folders"]:
            if folder["id"] == folder_id:
                folder["paused"] = paused
                self.replace_config(config)
                return
        raise SyncthingError(f"folder {folder_id!r} not found")

    def folder_status(self, folder_id: str) -> dict:
        return self._get(f"/rest/db/status?folder={folder_id}")

    def completion(self, folder_id: str, device_id: str) -> dict:
        """How much of `folder_id` the *specified remote device* still needs,
        from this device's point of view -- e.g. {"completion": 100.0,
        "needItems": 0, "needBytes": 0, ...}. This is the right call for "is
        my remote peer caught up", unlike folder_status's needFiles/needBytes,
        which describe what *this* (local) device needs and stay at 0 on a
        send-only folder regardless of whether any remote has received
        anything yet."""
        return self._get(f"/rest/db/completion?folder={folder_id}&device={device_id}")

    def connections(self) -> dict:
        return self._get("/rest/system/connections")


def write_ignore_file(folder_path: Path, ignore_source: Path) -> None:
    """Copy the shared .stignore into a synced folder's root. Safe to call
    repeatedly -- it only overwrites the .stignore file itself, never any
    other content in the folder."""
    folder_path.mkdir(parents=True, exist_ok=True)
    target = folder_path / ".stignore"
    target.write_text(ignore_source.read_text())


def resume_workspace_folder_if_mounted(client: SyncthingClient, folder_id: str, mount_path: str, required_uuid: str | None) -> bool:
    """The gate that stands between Syncthing and the SD-card-fallback
    problem: the Workspace folder is only ever resumed after confirming the
    external disk is actually mounted. Returns True if resumed, False (and
    leaves the folder paused) if the mount check fails."""
    try:
        require_mount(mount_path, required_uuid=required_uuid, what="workspace SSD")
    except MountSafetyError:
        client.set_folder_paused(folder_id, True)
        return False
    client.set_folder_paused(folder_id, False)
    return True


def resume_workspace_folder_for_state(client: SyncthingClient, folder_id: str, cfg) -> "WorkspaceState":
    """The state-aware version of the guard above: resumes the folder for
    PRODUCTION (real disk mounted) or TEST (bounded SD-card dir within its
    size cap) mode, and pauses it for PENDING. Use this instead of calling
    resume_workspace_folder_if_mounted directly once test mode is in play,
    so the same code path handles all three states without branching at
    every call site."""
    from .workspace_state import WorkspaceMode, resolve_workspace_state
    state = resolve_workspace_state(cfg)
    client.set_folder_paused(folder_id, not state.is_active)
    return state
