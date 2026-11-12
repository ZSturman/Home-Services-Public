import threading
import time

import pytest
from fastapi.testclient import TestClient

import workspace_util.dashboard.app as dashboard_app_mod
from workspace_util import backup as backup_mod
from workspace_util import storage_browser
from workspace_util.mount_safety import MountStatus

CONFIG_YAML = """
workspace:
  local_mac_path: "{mac_path}"
  pi_path: "{pi_path}"
  projects_subdir: "Projects"
  test_workspace:
    enabled: true
    path: "{pi_path}"
    max_bytes: 999999999
disk:
  workspace_mount: "{workspace_mount}"
  backup_mount: "{backup_mount}"
dashboard:
  port: 8787
  auth_token_env_var: "TEST_DASHBOARD_TOKEN"
  allowed_roots:
    - "{pi_path}"
logging:
  dir: "{log_dir}"
camera:
  storage_dir: "{camera_dir}"
commands:
  git_status:
    command: ["git", "status", "--short"]
    allowed_roots:
      - "{pi_path}"
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    pi_path = tmp_path / "Workspace"
    (pi_path / "Projects" / "Demo").mkdir(parents=True)
    (pi_path / "Projects" / "Demo" / "README.md").write_text("hello world")
    (pi_path / "secret_outside_root.txt").write_text("should not leak")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "top-secret.txt").write_text("nope")

    backup_mount = tmp_path / "backup-mount"
    backup_mount.mkdir()
    (backup_mount / "Documents").mkdir()
    (backup_mount / "Documents" / "a file ünicode.txt").write_text("read-only storage")
    (backup_mount / ".hidden").write_text("hidden")
    (backup_mount / "escape").symlink_to(outside, target_is_directory=True)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML.format(
        mac_path=str(tmp_path / "mac"),
        pi_path=str(pi_path),
        workspace_mount=str(tmp_path / "ws-mount"),
        backup_mount=str(backup_mount),
        log_dir=str(tmp_path / "logs"),
        camera_dir=str(tmp_path / "camera-storage"),
    ))

    monkeypatch.setenv("WORKSPACE_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("TEST_DASHBOARD_TOKEN", "s3cret-token")
    monkeypatch.setattr(
        storage_browser,
        "check_mount",
        lambda path, required_uuid=None: MountStatus(path=path, expected=True, mounted=True, device="test", fstype="tmpfs"),
    )
    dashboard_app_mod._config = None  # force reload against this test's config
    dashboard_app_mod._camera_service = None
    dashboard_app_mod._shutdown_cancel_event = None
    dashboard_app_mod._shutdown_requested_at = None

    yield TestClient(dashboard_app_mod.app), pi_path, outside

    if dashboard_app_mod._camera_service is not None:
        dashboard_app_mod._camera_service.stop()
    dashboard_app_mod._config = None
    dashboard_app_mod._camera_service = None
    dashboard_app_mod._shutdown_cancel_event = None
    dashboard_app_mod._shutdown_requested_at = None


def test_health_does_not_require_auth(client):
    c, _, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200


def test_dashboard_serves_one_operational_view_and_removes_legacy_route(client):
    c, _, _ = client

    dashboard = c.get("/")
    assert dashboard.status_code == 200
    assert "Raspberry Pi" in dashboard.text
    assert "HOME" in dashboard.text
    assert "CAMERA" in dashboard.text
    assert "Dashboard experiments" not in dashboard.text
    assert "Automations" not in dashboard.text

    legacy = c.get("/legacy")
    assert legacy.status_code == 404


def test_session_uses_secure_httponly_cookie(client):
    c, _, _ = client
    r = c.post("/api/session", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert "HttpOnly" in r.headers["set-cookie"]
    assert "Secure" in r.headers["set-cookie"]


def test_storage_browser_is_mount_bound_read_only_and_hides_escape(client):
    c, _, _ = client
    headers = {"X-Workspace-Token": "s3cret-token"}
    root = c.get("/api/storage/list", headers=headers)
    assert root.status_code == 200
    names = {entry["name"] for entry in root.json()["entries"]}
    assert "Documents" in names
    assert ".hidden" not in names
    assert "escape" not in names

    nested = c.get("/api/storage/list", params={"path": "Documents"}, headers=headers)
    assert nested.status_code == 200
    entry = nested.json()["entries"][0]
    assert entry["name"] == "a file ünicode.txt"
    assert entry["size_bytes"] == len("read-only storage")

    download = c.get("/api/storage/download", params={"path": entry["path"]}, headers=headers)
    assert download.status_code == 200
    assert download.content == b"read-only storage"
    assert "attachment" in download.headers["content-disposition"]
    assert "accept-ranges" in download.headers

    traversal = c.get("/api/storage/download", params={"path": "../outside/top-secret.txt"}, headers=headers)
    assert traversal.status_code == 403


def test_status_requires_auth_token(client):
    c, _, _ = client
    r = c.get("/api/status")
    assert r.status_code == 401


def test_status_with_wrong_token_rejected(client):
    c, _, _ = client
    r = c.get("/api/status", headers={"X-Workspace-Token": "wrong"})
    assert r.status_code == 401


def test_status_with_correct_token_succeeds(client):
    c, _, _ = client
    r = c.get("/api/status", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert "overall" in r.json()
    assert r.json()["workspace_state"]["projects_path"].endswith("Workspace/Projects")


def test_projects_listing(client):
    c, pi_path, _ = client
    r = c.get("/api/projects", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    names = [p["name"] for p in r.json()]
    assert names == ["Demo"]


def test_files_list_within_allowed_root(client):
    c, pi_path, _ = client
    r = c.get("/api/files", params={"path": str(pi_path / "Projects" / "Demo")},
              headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    names = {f["name"] for f in r.json()}
    assert "README.md" in names


def test_files_list_rejects_path_outside_allowed_roots(client):
    c, _, outside = client
    r = c.get("/api/files", params={"path": str(outside)},
              headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 403


def test_files_list_rejects_dotdot_traversal(client):
    c, pi_path, _ = client
    traversal = str(pi_path / "Projects" / "Demo" / ".." / ".." / "..")
    r = c.get("/api/files", params={"path": traversal},
              headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 403


def test_files_download_rejects_traversal(client):
    c, pi_path, _ = client
    traversal = str(pi_path / ".." / "outside" / "top-secret.txt")
    r = c.get("/api/files/download", params={"path": traversal},
              headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 403


def test_files_content_preview(client):
    c, pi_path, _ = client
    r = c.get("/api/files/content", params={"path": str(pi_path / "Projects" / "Demo" / "README.md")},
              headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert r.json()["text"] == "hello world"


def test_files_create_folder_within_allowed_root(client):
    c, pi_path, _ = client
    parent = pi_path / "Projects" / "Demo"
    r = c.post("/api/files/folders", params={"dir_path": str(parent), "name": "Assets"},
               headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 201
    assert (parent / "Assets").is_dir()


def test_files_create_folder_rejects_nested_or_traversal_name(client):
    c, pi_path, _ = client
    r = c.post("/api/files/folders", params={"dir_path": str(pi_path), "name": "../outside"},
               headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 400


def test_commands_run_rejects_cwd_outside_allowed_roots(client):
    c, _, outside = client
    r = c.post("/api/commands/git_status/run", params={"cwd": str(outside)},
               headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 403


def test_commands_run_rejects_unknown_command(client):
    c, pi_path, _ = client
    r = c.post("/api/commands/rm_rf_everything/run", params={"cwd": str(pi_path)},
               headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 404


def test_commands_run_allowed_command_succeeds(client):
    c, pi_path, _ = client
    r = c.post("/api/commands/git_status/run", params={"cwd": str(pi_path / "Projects" / "Demo")},
               headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert "exit_code" in r.json()


def test_storage_contract_endpoint(client):
    """The inter-project contract: this is what another project on the same
    Pi (e.g. Bidirectional Project Hub) is meant to call instead of reading
    /dev, /etc/fstab, or this project's config directly."""
    c, _, _ = client
    r = c.get("/api/storage/contract", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "test"  # this fixture's config.yaml enables test_workspace
    assert body["production_sync_safe"] is False
    assert body["project_directory_creation_allowed"] is False
    assert body["workspace_root"] is None


def test_backup_status_endpoint_is_authenticated(client):
    c, _, _ = client
    assert c.get("/api/backup/status").status_code == 401
    body = c.get("/api/backup/status", headers={"X-Workspace-Token": "s3cret-token"}).json()
    assert body["state"] == "unknown"


def test_automation_hub_reports_not_configured_without_contacting_a_host(client):
    c, _, _ = client
    r = c.get("/api/automation-hub", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert r.json() == {"configured": False, "automations": [], "runs": []}


def test_automation_hub_runs_only_a_valid_registered_id_with_control_plane_schema(client, monkeypatch):
    c, _, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.automation_hub.enabled = True
    cfg.automation_hub.control_plane_url = "http://127.0.0.1:8080"
    captured = {}

    class FakeResponse:
        status_code = 202
        text = ""

        def json(self):
            return {"id": "run-1", "status": "queued"}

    def fake_request(method, url, timeout=None, **kwargs):
        captured.update(method=method, url=url, timeout=timeout, kwargs=kwargs)
        return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "request", fake_request)

    r = c.post("/api/automation-hub/safe-test/run", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 202
    assert captured["url"] == "http://127.0.0.1:8080/api/v1/automations/safe-test/runs"
    assert captured["kwargs"]["json"] == {"trigger": "manual", "source": "dashboard"}


def test_camera_endpoints_are_authenticated_and_report_disabled_by_default(client):
    c, _, _ = client
    assert c.get("/api/camera/status").status_code == 401
    assert c.get("/api/camera/snapshot").status_code == 401

    status = c.get("/api/camera/status", headers={"X-Workspace-Token": "s3cret-token"})
    assert status.status_code == 200
    assert status.json()["enabled"] is False

    snapshot = c.get("/api/camera/snapshot", headers={"X-Workspace-Token": "s3cret-token"})
    assert snapshot.status_code == 503


def test_camera_latest_requires_auth_and_404s_before_any_capture(client):
    c, _, _ = client
    assert c.get("/api/camera/latest").status_code == 401
    r = c.get("/api/camera/latest", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 404


def test_camera_storage_endpoint_is_authenticated(client):
    c, _, _ = client
    assert c.get("/api/camera/storage").status_code == 401
    r = c.get("/api/camera/storage", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert "snapshot_count" in r.json()
    assert "low_space_warning" in r.json()


def test_camera_sync_endpoint_reports_not_configured_when_disabled(client):
    c, _, _ = client
    r = c.get("/api/camera/sync", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_out_of_house_toggle_requires_auth_and_persists(client):
    c, pi_path, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.logging.dir = str(pi_path.parent)
    headers = {"X-Workspace-Token": "s3cret-token"}

    assert c.get("/api/camera/out-of-house").status_code == 401
    assert c.post("/api/camera/out-of-house", json={"armed": True}).status_code == 401

    before = c.get("/api/camera/out-of-house", headers=headers)
    assert before.status_code == 200
    assert before.json()["armed"] is False

    armed = c.post("/api/camera/out-of-house", json={"armed": True}, headers=headers)
    assert armed.status_code == 200
    assert armed.json()["armed"] is True

    after = c.get("/api/camera/out-of-house", headers=headers)
    assert after.json()["armed"] is True

    disarmed = c.post("/api/camera/out-of-house", json={"armed": False}, headers=headers)
    assert disarmed.json()["armed"] is False


def test_camera_events_list_is_authenticated_and_empty_by_default(client):
    c, _, _ = client
    assert c.get("/api/camera/events").status_code == 401
    r = c.get("/api/camera/events", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert r.json() == []


def test_camera_event_detail_requires_auth_and_rejects_traversal(client):
    c, _, _ = client
    assert c.get("/api/camera/events/whatever").status_code == 401
    headers = {"X-Workspace-Token": "s3cret-token"}
    missing = c.get("/api/camera/events/does-not-exist", headers=headers)
    assert missing.status_code == 404
    traversal = c.get("/api/camera/events/..%2F..%2F..%2Fetc", headers=headers)
    assert traversal.status_code in (400, 404)


def test_camera_event_media_rejects_path_traversal_in_filename(client):
    c, _, _ = client
    headers = {"X-Workspace-Token": "s3cret-token"}
    r = c.get("/api/camera/events/evt-1/media/..%2F..%2F..%2Fetc%2Fpasswd", headers=headers)
    assert r.status_code in (400, 404)


def test_camera_snapshots_listing_requires_auth(client):
    c, _, _ = client
    assert c.get("/api/camera/snapshots").status_code == 401
    r = c.get("/api/camera/snapshots", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert r.json() == {"days": [], "snapshots": []}


def test_camera_snapshot_file_rejects_traversal(client):
    c, _, _ = client
    headers = {"X-Workspace-Token": "s3cret-token"}
    r = c.get("/api/camera/snapshots/file", params={"path": "../../../etc/passwd"}, headers=headers)
    assert r.status_code == 403


def test_status_endpoint_includes_camera_summary_when_disabled(client):
    c, _, _ = client
    r = c.get("/api/status", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert r.json()["camera"] == {"enabled": False}


def test_shutdown_requires_auth_is_disabled_by_default_and_needs_exact_confirmation(client):
    c, _, _ = client
    assert c.post("/api/system/shutdown", json={"confirmation": "SHUT DOWN PI"}).status_code == 401

    headers = {"X-Workspace-Token": "s3cret-token"}
    assert c.post("/api/system/shutdown", json={"confirmation": "SHUT DOWN PI"}, headers=headers).status_code == 403

    cfg = dashboard_app_mod.get_config()
    cfg.dashboard.shutdown_enabled = True
    assert c.post("/api/system/shutdown", json={"confirmation": "not it"}, headers=headers).status_code == 400


def test_shutdown_refuses_while_a_backup_is_active(client):
    c, pi_path, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.dashboard.shutdown_enabled = True
    cfg.logging.dir = str(pi_path.parent)
    backup_mod.backup_status_path(cfg.logging.dir).write_text(
        '{"state": "running", "active": true, "updated_at": ' + str(time.time()) + "}"
    )
    headers = {"X-Workspace-Token": "s3cret-token"}

    r = c.post("/api/system/shutdown", json={"confirmation": "SHUT DOWN PI"}, headers=headers)
    assert r.status_code == 409
    assert "backup is active" in r.json()["detail"].lower()


def test_shutdown_rejects_duplicate_request_while_one_is_pending(client):
    c, _, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.dashboard.shutdown_enabled = True
    cfg.dashboard.shutdown_delay_seconds = 60
    headers = {"X-Workspace-Token": "s3cret-token"}

    first = c.post("/api/system/shutdown", json={"confirmation": "SHUT DOWN PI"}, headers=headers)
    assert first.status_code == 202

    second = c.post("/api/system/shutdown", json={"confirmation": "SHUT DOWN PI"}, headers=headers)
    assert second.status_code == 409
    assert "already scheduled" in second.json()["detail"].lower()

    c.post("/api/system/shutdown/cancel", headers=headers)


def test_shutdown_never_accepts_a_user_supplied_command_or_argument(client, monkeypatch):
    """The entire argv passed to subprocess.run must be the fixed shutdown
    command -- nothing derived from the request body, headers, or query
    string ever reaches it. `shutdown_delay_seconds` is floored to 5 by the
    app (see _shutdown_status), so the actual post-delay action is exercised
    directly here rather than via a several-second sleep in the request path."""
    c, _, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.dashboard.shutdown_enabled = True
    headers = {"X-Workspace-Token": "s3cret-token"}
    captured = {}

    def fake_run(argv, check=False, timeout=None):
        captured["argv"] = argv

        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(dashboard_app_mod.subprocess, "run", fake_run)

    r = c.post(
        "/api/system/shutdown",
        json={"confirmation": "SHUT DOWN PI; rm -rf /", "extra": "ignored"},
        headers=headers,
    )
    assert r.status_code == 400  # the injected confirmation string itself must not match exactly

    r = c.post("/api/system/shutdown", json={"confirmation": "SHUT DOWN PI"}, headers=headers)
    assert r.status_code == 202
    assert c.get("/api/system/shutdown", headers=headers).json()["pending"] is True

    # Immediately cancel the *real* background timer this request just
    # started (floored to a 5s delay) -- a leftover real timer must never be
    # left running past this test, or it would call the *real* subprocess.run
    # a few seconds later, after monkeypatch has reverted it, including on a
    # real Pi test run. Then exercise the exact function that timer would
    # have invoked, directly and synchronously, against a fresh event.
    cancel = c.post("/api/system/shutdown/cancel", headers=headers)
    assert cancel.status_code == 200

    event = threading.Event()
    dashboard_app_mod._shutdown_cancel_event = event
    dashboard_app_mod._shutdown_when_safe(0, event)

    assert captured["argv"] == ["sudo", "-n", "/usr/bin/systemctl", "poweroff"]
    dashboard_app_mod._shutdown_cancel_event = None


def test_restarting_the_dashboard_process_leaves_no_pending_shutdown(client):
    """A fresh process (module-level globals reset, as happens on restart)
    must never resume a shutdown that was pending before the restart --
    fail-safe by construction, since the in-memory timer thread does not
    survive the process exiting."""
    c, _, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.dashboard.shutdown_enabled = True
    headers = {"X-Workspace-Token": "s3cret-token"}

    dashboard_app_mod._shutdown_cancel_event = None
    dashboard_app_mod._shutdown_requested_at = None

    status = c.get("/api/system/shutdown", headers=headers).json()
    assert status["pending"] is False


def test_shutdown_is_cancellable_and_never_accepts_user_supplied_command(client):
    c, _, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.dashboard.shutdown_enabled = True
    cfg.dashboard.shutdown_delay_seconds = 60
    headers = {"X-Workspace-Token": "s3cret-token"}

    r = c.post("/api/system/shutdown", json={"confirmation": "SHUT DOWN PI"}, headers=headers)
    assert r.status_code == 202
    assert r.json()["shutdown_in_seconds"] == 60
    assert c.get("/api/system/shutdown", headers=headers).json()["pending"] is True

    cancelled = c.post("/api/system/shutdown/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json() == {"cancelled": True}


def test_backup_status_prefers_completed_mac_backup(client):
    c, pi_path, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.logging.dir = str(pi_path.parent)
    backup_mod.mac_backup_status_path(cfg.logging.dir).write_text(
        '{"state": "completed", "active": false, "snapshot_id": "new-mac-snapshot"}'
    )

    body = c.get("/api/backup/status", headers={"X-Workspace-Token": "s3cret-token"}).json()
    assert body["state"] == "completed"
    assert body["snapshot_id"] == "new-mac-snapshot"


def test_dashboard_backup_starts_in_background_and_publishes_status_path(client, monkeypatch):
    c, pi_path, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.logging.dir = str(pi_path.parent)
    captured = {}

    def fake_run(source, backup_cfg, disk_cfg, tags=None, *, status_path=None):
        captured.update(source=source, tags=tags, status_path=status_path)

    monkeypatch.setattr(backup_mod, "run_backup", fake_run)

    r = c.post("/api/backup/run", headers={"X-Workspace-Token": "s3cret-token"})

    assert r.status_code == 202
    assert r.json()["accepted"] is True
    assert captured["source"] == str(pi_path)
    assert captured["tags"] == ["dashboard"]
    assert captured["status_path"] == backup_mod.backup_status_path(cfg.logging.dir)


def test_dashboard_backup_rejects_an_existing_active_job(client):
    c, pi_path, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.logging.dir = str(pi_path.parent)
    backup_mod.backup_status_path(cfg.logging.dir).write_text(
        '{"state": "running", "active": true, "updated_at": ' + str(time.time()) + "}"
    )

    r = c.post("/api/backup/run", headers={"X-Workspace-Token": "s3cret-token"})

    assert r.status_code == 409
    assert "already active" in r.json()["detail"]


def test_mac_status_disabled_returns_503(client):
    """mac_bridge.enabled defaults to False in this fixture's config -- the
    dashboard must say so plainly rather than trying to guess a host/port."""
    c, _, _ = client
    r = c.get("/api/mac/status", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 503


def test_mac_commands_run_proxies_to_mac_bridge(client, monkeypatch):
    """The dashboard -> Mac bridge routing this session's work is about:
    the dashboard must forward the call rather than executing anything
    itself, and must attach the Mac bridge's own token (never the caller's
    dashboard token) so an authenticated phone user can trigger only the
    Mac's own pre-approved commands."""
    c, pi_path, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.mac_bridge.enabled = True
    cfg.mac_bridge.host = "mac.example"
    cfg.mac_bridge.port = 8788
    monkeypatch.setenv(cfg.mac_bridge.token_env_var, "mac-bridge-secret")

    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"run_id": "abc", "exit_code": 0, "stdout": "ok", "stderr": "", "duration_seconds": 0.1, "timed_out": False}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["kwargs"] = kwargs
        return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "request", fake_request)

    r = c.post("/api/mac/commands/git_status/run", params={"cwd": str(pi_path)},
               headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert captured["url"] == "http://mac.example:8788/commands/git_status/run"
    assert captured["headers"]["X-Workspace-Token"] == "mac-bridge-secret"
    assert captured["kwargs"]["params"] == {"cwd": str(pi_path)}


def test_mac_files_list_proxies_to_mac_bridge(client, monkeypatch):
    """The Pi only proxies the fixed Mac-files API; the Mac re-validates the
    requested path within its configured Workspace before returning anything."""
    c, _, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.mac_bridge.enabled = True
    cfg.mac_bridge.host = "mac.example"
    monkeypatch.setenv(cfg.mac_bridge.token_env_var, "mac-bridge-secret")
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return [{"name": "Projects", "path": "/Users/example/Workspace/Projects", "is_dir": True}]

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured.update(method=method, url=url, headers=headers, kwargs=kwargs)
        return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "request", fake_request)

    r = c.get("/api/mac/files", params={"path": "/Users/example/Workspace"},
              headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 200
    assert r.json()[0]["name"] == "Projects"
    assert captured["method"] == "GET"
    assert captured["url"] == "http://mac.example:8788/files"
    assert captured["headers"]["X-Workspace-Token"] == "mac-bridge-secret"
    assert captured["kwargs"]["params"] == {"path": "/Users/example/Workspace"}


def test_mac_backup_run_proxies_to_fixed_mac_bridge_action(client, monkeypatch):
    c, pi_path, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.mac_bridge.enabled = True
    cfg.mac_bridge.host = "mac.example"
    cfg.logging.dir = str(pi_path.parent)
    monkeypatch.setenv(cfg.mac_bridge.token_env_var, "mac-bridge-secret")
    captured = {}

    class FakeResponse:
        status_code = 202
        text = ""

        def json(self):
            return {"accepted": True, "job": "com.workspace.mac-restic-backup"}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured.update(method=method, url=url, headers=headers, kwargs=kwargs)
        return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "request", fake_request)

    r = c.post("/api/mac/backup/run", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 202
    assert r.json()["accepted"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "http://mac.example:8788/backup/run"
    assert captured["headers"]["X-Workspace-Token"] == "mac-bridge-secret"


def test_mac_automation_run_proxies_only_a_valid_registered_id(client, monkeypatch):
    c, _, _ = client
    cfg = dashboard_app_mod.get_config()
    cfg.mac_bridge.enabled = True
    cfg.mac_bridge.host = "mac.example"
    monkeypatch.setenv(cfg.mac_bridge.token_env_var, "mac-bridge-secret")
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"accepted": True, "id": "social_drafts"}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        captured.update(method=method, url=url, headers=headers, kwargs=kwargs)
        return FakeResponse()

    import httpx
    monkeypatch.setattr(httpx, "request", fake_request)

    r = c.post("/api/mac/automations/social_drafts/run", headers={"X-Workspace-Token": "s3cret-token"})
    assert r.status_code == 202
    assert r.json()["accepted"] is True
    assert captured["url"] == "http://mac.example:8788/automations/social_drafts/run"

    rejected = c.post("/api/mac/automations/../run", headers={"X-Workspace-Token": "s3cret-token"})
    assert rejected.status_code == 404
