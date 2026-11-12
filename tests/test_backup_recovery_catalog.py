import hashlib
import os
import subprocess
from dataclasses import replace

import pytest

from home_services.backups.recovery import RecoveryCatalog
from home_services.config import ConfigError, load_config
from home_services.errors import Conflict, NotFound, ServiceError
from home_services.services import build_services
from home_services.worker import Stop, work_once


@pytest.fixture
def recovery_env(restic_env):
    e = restic_env
    repo = e.mount / "companion-repo"
    password = e.root / "companion-password"
    password.write_text("separate-companion-fixture-password")
    password.chmod(0o600)
    source = e.root / "current-mac-files"
    source.mkdir()
    (source / "recovery.txt").write_bytes(b"current Mac backup recovery\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
    env.update(RESTIC_REPOSITORY=str(repo), RESTIC_PASSWORD_FILE=str(password))
    for args in (["init"], ["backup", str(source), "--tag", "job-mac-workspace", "--json"]):
        subprocess.run([e.config.backup.restic_binary, *args], env=env, capture_output=True, check=True)
    volume = replace(e.config.volumes[0], roots={**e.config.volumes[0].roots, "companion_recovery": "companion-repo"})
    config = replace(e.config, volumes=(volume,), backup=replace(e.config.backup, companion_password_file=str(password)))
    services = build_services(config, clock=e.clock)
    original = e.s
    e.s = services
    yield e, source
    services.db.close()
    e.s = original


def test_current_and_legacy_snapshot_download_restore_and_scope(recovery_env):
    e, source = recovery_env
    c = e.client()
    h = e.token("backups:read", "backups:restore", "storage:read", "jobs:read")
    snapshots = c.get("/api/v1/backups/snapshots", headers=h).json()["snapshots"]
    assert {s["repository"] for s in snapshots} == {"primary", "companion"}
    snap = next(s for s in snapshots if s["repository"] == "companion")
    path = str(source / "recovery.txt")
    assert c.get(f"/api/v1/backups/snapshots/{snap['id']}/entries", params={"path": str(source)}, headers=h).json()["entries"][0]["name"] == "recovery.txt"
    data = c.get(f"/api/v1/backups/snapshots/{snap['short_id']}/content", params={"path": path}, headers=h)
    assert data.status_code == 200 and data.content == (source / "recovery.txt").read_bytes()
    denied = c.post("/api/v1/backups/restores", headers={**e.token("backups:read"), "Idempotency-Key": "denied"},
                    json={"snapshot_id": snap["id"], "include": [path]})
    assert denied.status_code == 403
    result = c.post("/api/v1/backups/restores", headers={**h, "Idempotency-Key": "restore-companion"},
                    json={"snapshot_id": snap["id"], "include": [path]})
    assert result.status_code == 201
    job = result.json()
    assert work_once(e.s, "backup", "fixture-worker", Stop())
    done = e.s.jobs.get(job["id"])
    assert done["state"] == "succeeded", done
    restored = e.files / done["result"]["destination"]["path"] / path.lstrip("/")
    assert hashlib.sha256(restored.read_bytes()).digest() == hashlib.sha256(data.content).digest()
    # A companion snapshot with the same tag/path cannot validate a primary writer receipt.
    primary, _ = e.s.backups.register_source(host="fixture-mac", path=str(source), tag="job-mac-workspace",
                                            scheduler="fixture", principal_id=None)
    run, _ = e.s.backups.start_run(primary["id"], scheduler="fixture", idempotency_key="primary-run")
    with pytest.raises(Conflict) as error:
        e.s.backups.finish(run["id"], state="succeeded", snapshot_id=snap["id"], summary=None, error=None)
    assert error.value.code == "snapshot_not_found"
    e.s.backups.finish(run["id"], state="failed", snapshot_id=None, summary=None, error={"code": "fixture"})
    assert c.get("/api/v1/backups/snapshots", params={"source_id": primary["id"]}, headers=h).json()["snapshots"] == []
    groups = e.s.backups.groups()
    assert any("job-mac-workspace" in g["tags"] and g["protection"]["evidence"] == "repository_snapshot" for g in groups)
    assert next(g for g in groups if "job-mac-workspace" in g["tags"])["source_id"] is None
    assert e.s.module_readiness()["backups:companion"]["state"] == "ready"
    assert e.s.guard.root_path("companion_recovery", write=False) == e.mount / "companion-repo"
    with pytest.raises(ServiceError):
        e.s.guard.root_path("companion_recovery", write=True)
    e.wrong_disk()
    assert e.s.module_readiness()["backups:companion"]["state"] == "unavailable"
    assert e.s.capabilities()["backups"]["snapshots"]["state"] == "unavailable"
    with pytest.raises(ServiceError):
        e.s.recovery.list_dir(snap["id"], str(source))


def test_configured_reader_failure_is_visible(recovery_env):
    e, _ = recovery_env
    (e.root / "companion-password").unlink()
    assert e.s.module_readiness()["backups:companion"]["state"] == "unavailable"
    r = e.client().get("/api/v1/backups/snapshots", headers=e.token("backups:read"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "repository_password_missing"


def test_ambiguous_prefix_and_unknown_snapshot_are_refused(env, monkeypatch):
    catalog = RecoveryCatalog(env.s.restic)
    catalog.readers["companion"] = env.s.restic
    ids = ["a" * 8 + "b" * 56, "a" * 8 + "c" * 56]
    monkeypatch.setattr(catalog, "snapshots", lambda **_: [{"id": i, "repository": "primary"} for i in ids])
    with pytest.raises(Conflict, match="ambiguous"):
        catalog.resolve("a" * 8)
    with pytest.raises(NotFound):
        catalog.resolve("d" * 64)
    assert catalog.resolve(ids[0])[1] == ids[0]


def test_companion_root_requires_separate_credentials(env):
    text = env.config_path.read_text()
    text = text.replace('restic_repository = "restic-repo"', 'restic_repository = "restic-repo"\ncompanion_recovery = "companion-repo"')
    assert "companion_recovery" in text
    env.config_path.write_text(text)
    with pytest.raises(ConfigError, match="configured together"):
        load_config(env.config_path)
