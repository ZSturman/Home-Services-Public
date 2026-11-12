from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import workspace_util.mac_bridge.app as mac_bridge_app_mod


CONFIG_YAML = """
workspace:
  local_mac_path: "{mac_path}"
  pi_path: "{pi_path}"
  projects_subdir: "Projects"
disk:
  workspace_mount: "{workspace_mount}"
  backup_mount: "{backup_mount}"
dashboard:
  port: 8787
mac_bridge:
  token_env_var: "TEST_MAC_BRIDGE_TOKEN"
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    mac_path = tmp_path / "MacWorkspace"
    project = mac_path / "Projects" / "Demo"
    project.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_YAML.format(
        mac_path=mac_path,
        pi_path=tmp_path / "PiWorkspace",
        workspace_mount=tmp_path / "workspace-mount",
        backup_mount=tmp_path / "backup-mount",
    ))
    monkeypatch.setenv("WORKSPACE_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("TEST_MAC_BRIDGE_TOKEN", "mac-token")
    mac_bridge_app_mod._config = None
    yield TestClient(mac_bridge_app_mod.app), project
    mac_bridge_app_mod._config = None


def headers():
    return {"X-Workspace-Token": "mac-token"}


def test_mac_bridge_can_create_folder_and_upload_only_in_workspace(client):
    c, project = client

    folder = c.post("/files/folders", params={"dir_path": str(project), "name": "Notes"}, headers=headers())
    assert folder.status_code == 201
    assert (project / "Notes").is_dir()

    upload = c.post(
        "/files/upload", params={"dir_path": str(project / "Notes")},
        files={"file": ("phone.txt", b"from phone", "text/plain")}, headers=headers(),
    )
    assert upload.status_code == 200
    assert (project / "Notes" / "phone.txt").read_text() == "from phone"

    blocked = c.post("/files/folders", params={"dir_path": str(project), "name": "../outside"}, headers=headers())
    assert blocked.status_code == 400


def test_mac_bridge_hammerspoon_calls_are_fixed_and_registered(client, monkeypatch):
    c, _ = client
    received = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = '[{"id":"social_drafts","title":"Social Drafts","available":true}]'

    def fake_run(argv, **kwargs):
        received.append(argv)
        return Result()

    monkeypatch.setattr(mac_bridge_app_mod.subprocess, "run", fake_run)
    listed = c.get("/automations", headers=headers())
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == "social_drafts"

    started = c.post("/automations/social_drafts/run", headers=headers())
    assert started.status_code == 202
    assert "runDashboardAction(\"social_drafts\")" in received[-1][-1]
