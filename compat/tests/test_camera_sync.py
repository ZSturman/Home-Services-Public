import pytest

from workspace_util import camera_sync
from workspace_util.config import CameraConfig, Config, DashboardConfig, DiskConfig, LoggingConfig, MacBridgeConfig, \
    NotificationsConfig, SyncConfig, WorkspaceConfig
from workspace_util.config import BackupConfig


def make_config(tmp_path, sync_enabled=True) -> Config:
    return Config(
        workspace=WorkspaceConfig(local_mac_path=str(tmp_path / "mac"), pi_path=str(tmp_path / "pi")),
        disk=DiskConfig(), sync=SyncConfig(), backup=BackupConfig(),
        dashboard=DashboardConfig(), mac_bridge=MacBridgeConfig(),
        notifications=NotificationsConfig(), logging=LoggingConfig(dir=str(tmp_path / "logs")),
        commands={}, source_path=tmp_path / "config.yaml",
        camera=CameraConfig(sync_enabled=sync_enabled, storage_dir=str(tmp_path / "camstore")),
    )


class FakeSyncthingClient:
    def __init__(self, config, my_id="PI-ID", connections=None, folder_status_response=None,
                 completion_response=None):
        self._config = config
        self._my_id = my_id
        self._connections = connections or {"connections": {}}
        self._folder_status = folder_status_response or {"needFiles": 0, "state": "idle"}
        self._completion = completion_response or {"needItems": 0, "needBytes": 0, "completion": 100.0}

    def get_config(self):
        return self._config

    def replace_config(self, config):
        self._config = config

    def my_device_id(self):
        return self._my_id

    def connections(self):
        return self._connections

    def folder_status(self, folder_id):
        return self._folder_status

    def completion(self, folder_id, device_id):
        return self._completion

    def upsert_folder(self, folder_id, label, path, device_ids, paused=True, ignore_perms=False):
        existing = next((f for f in self._config["folders"] if f["id"] == folder_id), None)
        entry = {"id": folder_id, "label": label, "path": path,
                 "devices": [{"deviceID": d} for d in device_ids], "paused": paused, "type": "sendreceive"}
        if existing:
            existing.update(entry)
        else:
            self._config["folders"].append(entry)

    def set_folder_paused(self, folder_id, paused):
        for folder in self._config["folders"]:
            if folder["id"] == folder_id:
                folder["paused"] = paused


def test_folder_sync_status_reports_not_configured_when_sync_disabled(tmp_path):
    cfg = make_config(tmp_path, sync_enabled=False)
    status = camera_sync.folder_sync_status(cfg)
    assert status["configured"] is False


def test_folder_sync_status_reports_not_configured_when_folder_missing(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, sync_enabled=True)
    fake = FakeSyncthingClient({"devices": [], "folders": []})
    monkeypatch.setattr(camera_sync.SyncthingClient, "from_local_config", classmethod(lambda cls: fake))
    status = camera_sync.folder_sync_status(cfg)
    assert status["configured"] is False
    assert "not set up" in status["reason"]


def test_folder_sync_status_reports_mac_reachable_and_pending_count(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, sync_enabled=True)
    config = {
        "devices": [],
        "folders": [{
            "id": "camera", "paused": False,
            "devices": [{"deviceID": "PI-ID"}, {"deviceID": "MAC-ID"}],
        }],
    }
    fake = FakeSyncthingClient(
        config, my_id="PI-ID",
        connections={"connections": {"MAC-ID": {"connected": True, "at": "2026-09-10T00:00:00Z"}}},
        folder_status_response={"needFiles": 0, "needBytes": 0, "state": "syncing", "errors": 0},
        completion_response={"needItems": 3, "needBytes": 12345, "completion": 91.2},
    )
    monkeypatch.setattr(camera_sync.SyncthingClient, "from_local_config", classmethod(lambda cls: fake))

    status = camera_sync.folder_sync_status(cfg)
    assert status["configured"] is True
    assert status["mac_reachable"] is True
    assert status["need_files"] == 3  # from completion(), not the (always-0-on-sendonly) folder_status
    assert status["state"] == "syncing"


def test_folder_sync_status_reports_mac_unreachable(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, sync_enabled=True)
    config = {
        "devices": [],
        "folders": [{"id": "camera", "paused": False, "devices": [{"deviceID": "PI-ID"}, {"deviceID": "MAC-ID"}]}],
    }
    fake = FakeSyncthingClient(config, my_id="PI-ID", connections={"connections": {"MAC-ID": {"connected": False}}})
    monkeypatch.setattr(camera_sync.SyncthingClient, "from_local_config", classmethod(lambda cls: fake))

    status = camera_sync.folder_sync_status(cfg)
    assert status["mac_reachable"] is False


def test_folder_sync_status_falls_back_to_zero_pending_if_completion_call_fails(tmp_path, monkeypatch):
    """A remote that has never connected can make Syncthing's own
    /rest/db/completion error out -- that must degrade to 0 pending rather
    than raising and breaking the whole status response."""
    cfg = make_config(tmp_path, sync_enabled=True)
    config = {
        "devices": [],
        "folders": [{"id": "camera", "paused": False, "devices": [{"deviceID": "PI-ID"}, {"deviceID": "MAC-ID"}]}],
    }
    fake = FakeSyncthingClient(config, my_id="PI-ID", connections={"connections": {}})

    def boom(self, folder_id, device_id):
        raise RuntimeError("no completion data yet")

    monkeypatch.setattr(FakeSyncthingClient, "completion", boom)
    monkeypatch.setattr(camera_sync.SyncthingClient, "from_local_config", classmethod(lambda cls: fake))

    status = camera_sync.folder_sync_status(cfg)
    assert status["configured"] is True
    assert status["need_files"] == 0


def test_folder_sync_status_never_raises_when_syncthing_unavailable(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, sync_enabled=True)

    def boom(cls):
        raise RuntimeError("syncthing not running")

    monkeypatch.setattr(camera_sync.SyncthingClient, "from_local_config", classmethod(boom))
    status = camera_sync.folder_sync_status(cfg)
    assert status["configured"] is False
    assert "unavailable" in status["reason"]


def test_setup_pi_side_creates_sendonly_folder(tmp_path):
    fake = FakeSyncthingClient({"devices": [], "folders": []})
    camera_sync.setup_pi_side(fake, "MAC-ID", str(tmp_path / "camstore"))
    folder = fake._config["folders"][0]
    assert folder["id"] == "camera"
    assert folder["type"] == "sendonly"
    assert folder["paused"] is True


def test_setup_mac_side_creates_receiveonly_folder(tmp_path):
    fake = FakeSyncthingClient({"devices": [], "folders": []})
    camera_sync.setup_mac_side(fake, "PI-ID", str(tmp_path / "mac-camera-sync"))
    folder = fake._config["folders"][0]
    assert folder["id"] == "camera"
    assert folder["type"] == "receiveonly"


def test_resume_if_storage_ready_stays_paused_when_dir_missing(tmp_path):
    fake = FakeSyncthingClient({"devices": [], "folders": [{"id": "camera", "paused": True, "devices": []}]})
    resumed = camera_sync.resume_if_storage_ready(fake, str(tmp_path / "does-not-exist"))
    assert resumed is False
    assert fake._config["folders"][0]["paused"] is True


def test_resume_if_storage_ready_resumes_when_dir_exists(tmp_path):
    storage = tmp_path / "camstore"
    storage.mkdir()
    fake = FakeSyncthingClient({"devices": [], "folders": [{"id": "camera", "paused": True, "devices": []}]})
    resumed = camera_sync.resume_if_storage_ready(fake, str(storage))
    assert resumed is True
    assert fake._config["folders"][0]["paused"] is False
