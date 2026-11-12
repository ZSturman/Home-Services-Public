import os

import pytest

from workspace_util.config import ConfigError, load_config, load_raw, resolve_role, set_disk_uuid

MINIMAL_YAML = """
workspace:
  local_mac_path: "/tmp/mac-workspace"
  pi_path: "/tmp/pi-workspace"
disk:
  workspace_mount: "/mnt/workspace"
  backup_mount: "/mnt/workspace-backup"
dashboard:
  port: 8787
commands:
  git_status:
    command: ["git", "status", "--short"]
    allowed_roots: ["/tmp/pi-workspace"]
"""


def test_load_config_basic(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML)
    cfg = load_config(cfg_path)
    assert cfg.workspace.local_mac_path == "/tmp/mac-workspace"
    assert cfg.dashboard.port == 8787
    assert "git_status" in cfg.commands
    assert cfg.commands["git_status"].command == ["git", "status", "--short"]


def test_missing_workspace_section_raises(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("disk:\n  workspace_mount: /mnt/workspace\n")
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_env_override_applied(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML)
    monkeypatch.setenv("WORKSPACE_DASHBOARD__PORT", "9999")
    cfg = load_config(cfg_path)
    assert cfg.dashboard.port == 9999


def test_env_override_bool(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML + "\nsync:\n  enabled: true\n")
    monkeypatch.setenv("WORKSPACE_SYNC__ENABLED", "false")
    cfg = load_config(cfg_path)
    assert cfg.sync.enabled is False


def test_literal_secret_value_rejected(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML + '\nmac_bridge:\n  token: "sk-super-secret-value"\n')
    with pytest.raises(ConfigError, match="secret"):
        load_raw(cfg_path)


def test_env_var_pointer_for_secret_is_allowed(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML + "\nmac_bridge:\n  token_env_var: WORKSPACE_MAC_BRIDGE_TOKEN\n")
    data = load_raw(cfg_path)  # must not raise
    assert data["mac_bridge"]["token_env_var"] == "WORKSPACE_MAC_BRIDGE_TOKEN"


def test_test_workspace_config_parsed(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("""
workspace:
  local_mac_path: "/tmp/mac-workspace"
  pi_path: "/tmp/pi-workspace"
  test_workspace:
    enabled: true
    path: "/opt/workspace-util/test-workspace"
    max_bytes: 1000
""")
    cfg = load_config(cfg_path)
    assert cfg.workspace.test_workspace.enabled is True
    assert cfg.workspace.test_workspace.max_bytes == 1000


def test_test_workspace_defaults_disabled(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML)
    cfg = load_config(cfg_path)
    assert cfg.workspace.test_workspace.enabled is False
    assert cfg.workspace.test_workspace.max_bytes == 536_870_912


def test_camera_config_defaults_disabled_and_parses(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML + """
camera:
  enabled: true
  device: /dev/video9
  width: 800
  height: 600
  refresh_seconds: 12
""")
    cfg = load_config(cfg_path)
    assert cfg.camera.enabled is True
    assert cfg.camera.device == "/dev/video9"
    assert cfg.camera.width == 800
    assert cfg.camera.refresh_seconds == 12


def test_automation_hub_defaults_disabled_and_parses(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML + """
automation_hub:
  enabled: true
  control_plane_url: http://127.0.0.1:3000
""")
    cfg = load_config(cfg_path)
    assert cfg.automation_hub.enabled is True
    assert cfg.automation_hub.control_plane_url == "http://127.0.0.1:3000"


def test_config_file_env_var_used(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML)
    monkeypatch.setenv("WORKSPACE_CONFIG_FILE", str(cfg_path))
    cfg = load_config()
    assert cfg.workspace.pi_path == "/tmp/pi-workspace"


def test_resolve_role_explicit_wins(monkeypatch):
    assert resolve_role("mac") == "mac"
    assert resolve_role("pi") == "pi"
    assert resolve_role("PI") == "pi"  # case-insensitive


def test_resolve_role_auto_infers_from_platform(monkeypatch):
    import workspace_util.config as config_mod
    monkeypatch.setattr(config_mod.platform, "system", lambda: "Linux")
    assert resolve_role("auto") == "pi"
    monkeypatch.setattr(config_mod.platform, "system", lambda: "Darwin")
    assert resolve_role("auto") == "mac"


def test_load_config_defaults_role_to_auto(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML)
    cfg = load_config(cfg_path)
    assert cfg.role in ("mac", "pi")  # resolved, never left as "auto"


def test_load_config_role_explicit_in_yaml(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML + "\nrole: pi\n")
    cfg = load_config(cfg_path)
    assert cfg.role == "pi"


def test_set_disk_uuid_rewrites_only_that_line(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML + '\ndisk:\n  workspace_mount: "/mnt/workspace"\n  workspace_uuid: ""\n  backup_uuid: "existing-backup-uuid"\n')
    set_disk_uuid(cfg_path, "workspace", "abcd-1234")
    text = cfg_path.read_text()
    assert 'workspace_uuid: "abcd-1234"' in text
    assert 'backup_uuid: "existing-backup-uuid"' in text  # untouched
    assert "local_mac_path" in text  # rest of the file untouched


def test_set_disk_uuid_missing_key_raises(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML)  # no disk.workspace_uuid line at all
    with pytest.raises(ConfigError, match="workspace_uuid"):
        set_disk_uuid(cfg_path, "workspace", "abcd-1234")


def test_set_disk_uuid_rejects_unknown_role(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_YAML + '\ndisk:\n  workspace_uuid: ""\n')
    with pytest.raises(ConfigError, match="role"):
        set_disk_uuid(cfg_path, "backup-hdd", "abcd-1234")
