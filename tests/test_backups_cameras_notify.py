import ast
import json
import shutil
from pathlib import Path

import httpx
import pytest

from home_services.cameras.events import EventGrouper
from home_services.errors import CapabilityUnavailable, Conflict, ServiceError
from home_services.legacy import FIELDS, build_storage_contract

SRC = Path(__file__).resolve().parents[1] / "src" / "home_services"
PREDECESSOR_FIELDS = {"mode", "production_sync_safe", "workspace_disk_mounted", "workspace_root", "syncthing_workspace_folder",
                      "storage_health", "storage_health_detail", "project_directory_creation_allowed", "detail"}


def _repo_present(env):
    (env.mount / "restic-repo" / "data").mkdir(exist_ok=True)
    (env.mount / "restic-repo" / "config").write_text("fixture")


def _source(env, path="/Users/me/Workspace"):
    return env.s.backups.register_source(host="mac", path=path, tag="mac-workspace", scheduler="files-backups-companion", principal_id=None)[0]


def test_run_refused_without_repository_and_never_initializes(env):
    src = _source(env)
    with pytest.raises(ServiceError) as e:
        env.s.backups.start_run(src["id"], scheduler="files-backups-companion", idempotency_key="k")
    assert e.value.code == "repository_unavailable"
    assert not (env.mount / "restic-repo" / "config").exists()


def test_no_code_path_runs_restic_init_forget_or_prune():
    for py in SRC.rglob("*.py"):
        if py.name == "devfixtures.py":  # creates disposable fixture repositories only
            continue
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, (ast.List, ast.Tuple)):
                words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                assert not {"init", "forget", "prune"} & set(words), f"{py} builds a destructive restic argv: {words}"


def _fixture_source(e):
    return e.s.backups.register_source(host="fixture-mac", path=str(e.root.resolve() / "backup-source"), tag="mac-workspace",
                                       scheduler="files-backups-companion", principal_id=None)[0]


def test_stale_run_interrupted_and_cannot_override_newer_success(restic_env):
    e = restic_env
    src = _fixture_source(e)
    snap = e.s.restic.snapshots()[0]
    old, _ = e.s.backups.start_run(src["id"], scheduler="files-backups-companion", idempotency_key="a")
    e.clock.advance(e.config.backup.lease_seconds + 1)
    new, _ = e.s.backups.start_run(src["id"], scheduler="files-backups-companion", idempotency_key="b")
    assert e.s.backups.run(old["id"])["state"] == "interrupted"
    done = e.s.backups.finish(new["id"], state="succeeded", snapshot_id=snap["short_id"], summary=None, error=None)
    assert done["snapshot_id"] == snap["id"]  # stored as the repository's full ID
    with pytest.raises(Conflict):
        e.s.backups.finish(old["id"], state="failed", snapshot_id=None, summary=None, error={"m": "late"})
    assert e.s.backups.source(src["id"])["protection"]["snapshot_id"] == snap["id"]


def test_succeeded_requires_a_real_matching_snapshot(restic_env):
    e = restic_env
    src = _fixture_source(e)
    other = e.s.backups.register_source(host="fixture-mac", path="/elsewhere", tag="other", scheduler="files-backups-companion",
                                        principal_id=None)[0]
    run, _ = e.s.backups.start_run(src["id"], scheduler="files-backups-companion", idempotency_key="1")
    with pytest.raises(Conflict) as err:
        e.s.backups.finish(run["id"], state="succeeded", snapshot_id="ab" * 32, summary=None, error=None)
    assert err.value.code == "snapshot_not_found"
    e.s.backups.finish(run["id"], state="failed", snapshot_id=None, summary=None, error={"code": "x"})
    run2, _ = e.s.backups.start_run(other["id"], scheduler="files-backups-companion", idempotency_key="2")
    with pytest.raises(Conflict) as err:
        e.s.backups.finish(run2["id"], state="succeeded", snapshot_id=e.s.restic.snapshots()[0]["id"], summary=None, error=None)
    assert err.value.code == "snapshot_mismatch"
    assert e.s.backups.run(run2["id"])["state"] == "running"  # still live; the companion can report the real outcome
    assert e.s.backups.source(other["id"])["protection"]["state"] == "none"


def test_single_writer_and_single_scheduler(env):
    _repo_present(env)
    a, b = _source(env), _source(env, "/Users/me/Other")
    env.s.backups.start_run(a["id"], scheduler="files-backups-companion", idempotency_key="1")
    with pytest.raises(Conflict, match="repository lease"):
        env.s.backups.start_run(b["id"], scheduler="files-backups-companion", idempotency_key="2")
    with pytest.raises(Conflict, match="another scheduler"):
        env.s.backups.register_source(host="mac", path="/Users/me/Workspace", tag="x", scheduler="legacy", principal_id=None)


def test_protection_comes_from_repository_snapshots_and_goes_overdue(restic_env):
    from home_services.backups.restic import snapshot_timestamp
    e = restic_env
    src = _fixture_source(e)  # no run was ever reported: the snapshot alone is the evidence
    snap = e.s.restic.snapshots()[0]
    e.clock.t = snapshot_timestamp(snap["time"]) + 3600
    p = e.s.backups.source(src["id"])["protection"]
    assert p["state"] == "current" and p["evidence"] == "repository_snapshot"
    e.clock.advance(26 * 3600)
    e.s.restic._cache = None
    assert e.s.backups.source(src["id"])["protection"]["state"] == "overdue"
    groups = e.s.backups.groups()
    assert len(groups) == 1 and groups[0]["source_id"] == src["id"] and groups[0]["protection"]["state"] == "overdue"


def test_backup_reports_unknown_protection_without_runs(env):
    assert _source(env)["protection"]["state"] == "unknown"


def test_event_grouping_quiet_cap_and_cooldown():
    g = EventGrouper()
    ev, _ = g.motion("cam", 0)
    assert ev.notify
    g.motion("cam", 15)  # within 20 s quiet window: same event
    _, closed = g.motion("cam", 40)  # 25 s after last motion: closes, new event
    assert closed[0].id == ev.id and closed[0].ended_at == 35 and closed[0].end_reason == "quiet"
    second = g._open["cam"]
    assert not second.notify  # 40 s after the first notification: within 120 s cooldown
    for t in range(41, 400, 10):
        g.motion("cam", t)
    assert second.ended_at == 40 + 300 and second.end_reason == "max_duration"
    assert g._open["cam"].notify  # 340 s after first notify


def test_cameras_default_disarmed_and_arming_refused(env):
    cams = env.s.cameras.list()
    assert cams and all(c["armed"] is False for c in cams)
    assert cams[0]["capabilities"]["recording"]["reason"] == "engine_not_configured"
    with pytest.raises(CapabilityUnavailable):
        env.s.cameras.set_armed("workspace-pi-camera", True)
    assert env.s.cameras.set_armed("workspace-pi-camera", False)["armed"] is False


def test_outbox_dedupes_retries_and_hides_url(env, monkeypatch):
    msg, created = env.s.outbox.enqueue("evt_1", "Motion", "Front door")
    _, again = env.s.outbox.enqueue("evt_1", "Motion", "Front door")
    assert created and not again
    secret_url = "https://ntfy.example/private-topic-123"
    calls = []

    def failing(request):
        calls.append(request)
        raise httpx.ConnectError("boom", request=request)

    with httpx.Client(transport=httpx.MockTransport(failing)) as c:
        env.s.outbox.deliver_due(c, secret_url)
    m = env.s.outbox.list()[0]
    assert m["state"] == "pending" and m["attempts"] == 1 and "private-topic" not in (m["last_error"] or "")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        assert env.s.outbox.deliver_due(c, secret_url)["delivered"] == 0  # backoff not yet elapsed
        env.clock.advance(16)
        assert env.s.outbox.deliver_due(c, secret_url)["delivered"] == 1
    assert env.s.outbox.list()[0]["state"] == "delivered"


def test_outbox_marks_failed_after_max_attempts(env):
    env.s.outbox.enqueue("evt_2", "Motion", "x")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
        for _ in range(env.config.notifications.max_attempts):
            env.s.outbox.deliver_due(c, "https://ntfy.example/t")
            env.clock.advance(3601)
    assert env.s.outbox.summary()["failed"] == 1


def test_legacy_contract_fields_match_predecessor(env):
    body = build_storage_contract(env.config, env.s.guard)
    assert set(body) == set(FIELDS) == PREDECESSOR_FIELDS
    assert body["production_sync_safe"] is False and body["project_directory_creation_allowed"] is False
    env.unplug()
    body = build_storage_contract(env.config, env.s.guard)
    assert body["storage_health"] in ("warning", "error")


def test_monitor_announces_volume_loss_once_after_grace_then_recovery(env):
    from home_services.monitor import Monitor
    m = Monitor(env.s)
    assert m.tick(include_backups=False)["enqueued"] == 0
    env.unplug()
    assert m.tick(include_backups=False)["enqueued"] == 0  # within the grace period: a brief blip is not announced
    env.clock.advance(121)
    assert m.tick(include_backups=False)["enqueued"] == 1
    env.clock.advance(60)
    assert m.tick(include_backups=False)["enqueued"] == 0  # one message per episode
    env.replug()
    assert m.tick(include_backups=False)["enqueued"] == 1
    titles = [x["title"] for x in env.s.outbox.list()]
    assert titles == ["Storage disk available again", "Storage disk unavailable"]
    env.unplug()
    m.tick(include_backups=False)
    env.replug()
    assert m.tick(include_backups=False)["enqueued"] == 0  # cleared within grace: nothing to recover from
    for msg in env.s.db.all("SELECT body FROM outbox"):
        assert str(env.mount) not in msg["body"] and "/" not in msg["body"].replace("Restores/", "")


def test_monitor_reports_finished_restore_and_overdue_series(restic_env, monkeypatch):
    from home_services import monitor
    from home_services.backups.restic import snapshot_timestamp
    from home_services.monitor import Monitor
    monkeypatch.setattr(monitor, "LOW_STATE_STORE_BYTES", 0)  # test temp dirs may sit on a small tmpfs
    e = restic_env
    m = Monitor(e.s)
    e.clock.t = snapshot_timestamp(e.s.restic.snapshots()[0]["time"]) + 60
    assert m.tick()["enqueued"] == 0
    snap = e.s.restic.snapshots()[0]
    job, _ = e.s.jobs.create("backup.restore", {"snapshot_id": snap["id"], "include": [snap["paths"][0] + "/Projects/Demo"]},
                             idempotency_key="r1")
    e.clock.advance(5)
    from home_services.worker import Stop, work_once
    work_once(e.s, "backup", "w", Stop())
    assert e.s.jobs.get(job["id"])["state"] == "succeeded"
    e.clock.advance(5)
    assert m.tick()["enqueued"] == 1
    assert m.tick()["enqueued"] == 0
    e.clock.advance(27 * 3600)
    e.s.restic._cache = None
    out = m.tick()
    assert out["enqueued"] == 1 and any(k.startswith("backup_overdue:") for k in out["active_problems"])
    msgs = e.s.outbox.list()
    assert msgs[0]["title"] == "Backup overdue" and msgs[1]["title"] == "Restore finished"
    assert str(e.root) not in json.dumps([dict(r) for r in e.s.db.all("SELECT title, body FROM outbox")])


def test_ntfy_url_never_reaches_logs(env, caplog, monkeypatch):
    import logging
    from home_services.cli import quiet_third_party_loggers
    quiet_third_party_loggers()
    secret_url = "https://ntfy.example/hs-secret-topic-123"
    env.s.outbox.enqueue("k", "t", "b")
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    with caplog.at_level(logging.DEBUG), httpx.Client(transport=transport) as client:
        env.s.outbox.deliver_due(client, secret_url)
    with caplog.at_level(logging.INFO), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        env.clock.advance(3600)
        env.s.outbox.deliver_due(client, secret_url)
    assert "hs-secret-topic" not in caplog.text
    assert "hs-secret-topic" not in json.dumps(env.s.outbox.list())


def test_backup_problems_survive_ticks_that_skip_backup_checks(env, monkeypatch):
    from home_services.monitor import Monitor, Problem
    m = Monitor(env.s)
    monkeypatch.setattr(m, "_backup_problems", lambda: [Problem("backup_empty:h:t", "Backup contains no files", "x", grace_seconds=0,
                                                                recovered_title="ok", recovered_body="ok")])
    base = m.problems  # the fixture config has backups disabled; feed the backup problem in directly
    m.problems = lambda include_backups: base(include_backups=False) + (m._backup_problems() if include_backups else [])
    assert m.tick(include_backups=True)["enqueued"] == 1
    for _ in range(3):
        assert m.tick(include_backups=False)["enqueued"] == 0
    assert [x["title"] for x in env.s.outbox.list()] == ["Backup contains no files"]


def test_external_repository_source_is_status_only_with_its_own_lease(restic_env, monkeypatch):
    from home_services import monitor
    from home_services.monitor import Monitor
    monkeypatch.setattr(monitor, "LOW_STATE_STORE_BYTES", 0)
    e = restic_env
    ext, _ = e.s.backups.register_source(host="mac", path="/Users/me/Workspace/Projects", tag="job-mac-projects",
                                         scheduler="files-backups-companion", principal_id=None,
                                         repository="external:files-backups/mac-projects")
    assert ext["repository"] == "external:files-backups/mac-projects" and ext["protection"]["state"] == "none"
    with pytest.raises(ServiceError):
        e.s.backups.register_source(host="mac", path="/x", tag="t", scheduler="c", principal_id=None, repository="/etc/passwd")
    prim = _fixture_source(e)
    p_run, _ = e.s.backups.start_run(prim["id"], scheduler="files-backups-companion", idempotency_key="p")
    run, _ = e.s.backups.start_run(ext["id"], scheduler="files-backups-companion", idempotency_key="x")  # separate lease
    assert e.s.backups.lease()["holder_id"] == p_run["id"]
    with pytest.raises(Conflict, match="repository lease"):
        e.s.backups.start_run(ext["id"], scheduler="files-backups-companion", idempotency_key="y")
    done = e.s.backups.finish(run["id"], state="succeeded", snapshot_id="ab" * 32,
                              summary={"total_files": 10, "total_bytes": 2048}, error=None)  # not in any readable repo: accepted as reported
    assert done["state"] == "succeeded"
    p = e.s.backups.source(ext["id"])["protection"]
    assert p["state"] == "current" and p["evidence"] == "companion_report" and p["verified"] is False and p["contents"]["files"] == 10
    assert all(g["source_id"] != ext["id"] for g in e.s.backups.groups())  # never mixed with repository evidence
    m = Monitor(e.s)
    m.tick()
    e.clock.advance(27 * 3600)
    e.s.restic._cache = None
    assert f"backup_overdue:{ext['id']}" in m.tick()["active_problems"]


def test_observation_record_and_report(env):
    from home_services.observe import INTERVAL_SECONDS, load_samples, record, summarize
    sample = record(str(env.config_path))
    assert set(sample) >= {"units", "predecessor", "doctor", "host", "readiness", "cameras", "outbox"}
    text = json.dumps(sample)
    assert str(env.mount) not in text and "hst_" not in text
    samples = load_samples(env.config.data_dir, hours=1)
    assert len(samples) == 1
    good = {"ts": 0, "at": "t0", "units": {"u": {"active": "active", "restarts": 0}}, "predecessor": {}, "doctor": {},
            "host": {"mem_available": 2e9, "load5": 0.5}, "cameras": {"c": {"covered_seconds": INTERVAL_SECONDS, "motion_events": 1}},
            "outbox": {"failed": 0}, "jobs_failed": 0, "backups": []}
    later = json.loads(json.dumps(good)) | {"ts": 900, "at": "t1", "external_sources": [
        {"id": "src_1", "host": "mac", "tag": "job-mac-workspace", "state": "current", "age_hours": 3.0, "last_run": "succeeded"}]}
    lines, problems = summarize([good, later])
    assert problems == [] and "external source mac job-mac-workspace: current, newest 3.0 h old, last run succeeded" in lines
    bad = json.loads(json.dumps(later)) | {"ts": 1800, "at": "t2"}
    bad["units"]["u"] = {"active": "failed", "restarts": 3}
    bad["cameras"]["c"]["covered_seconds"] = 100
    bad2 = json.loads(json.dumps(bad)) | {"ts": 2700, "at": "t3"}
    problems = summarize([good, later, bad, bad2])[1]
    assert any("u not always active" in p for p in problems) and any("coverage" in p for p in problems)


def test_retired_series_is_listed_but_raises_no_alerts(restic_env, monkeypatch):
    from dataclasses import replace

    from home_services import monitor
    from home_services.backups.restic import snapshot_timestamp
    from home_services.config import RetiredSeries
    from home_services.legacy import build_backup_status
    from home_services.monitor import Monitor
    monkeypatch.setattr(monitor, "LOW_STATE_STORE_BYTES", 0)
    e = restic_env
    e.clock.t = snapshot_timestamp(e.s.restic.snapshots()[0]["time"]) + 27 * 3600
    assert "Overdue: fixture-mac" in build_backup_status(e.s)["warning"]
    m = Monitor(e.s)
    assert m.tick()["enqueued"] == 1  # "Backup overdue" while the series is still expected
    e.s.backups.config = replace(e.config, backup=replace(e.config.backup, retired_series=(
        RetiredSeries(host="fixture-mac", tag="mac-workspace", reason="replaced by the companion"),)))
    [g] = e.s.backups.groups()
    assert g["protection"]["state"] == "overdue"  # still the honest age of the newest snapshot
    assert g["retired"] == {"reason": "replaced by the companion"}
    assert m.tick() == {"enqueued": 0, "active_problems": []}  # retiring it is not "current again"
    assert [r["title"] for r in e.s.outbox.list()] == ["Backup overdue"]
    status = build_backup_status(e.s)
    assert status["warning"] is None and status["series"] == 1 and status["state"] == "completed"
    assert e.s.restic.snapshots()  # nothing about the repository changed


def test_retired_series_config(tmp_path):
    from home_services.config import load_config
    from home_services.contract import _fixture_config
    path = _fixture_config(tmp_path)
    path.write_text(path.read_text() + '\n[[backup.retired_series]]\nhost = "mac"\ntag = "mac-workspace"\nreason = "moved"\n')
    [r] = load_config(path).backup.retired_series
    assert (r.host, r.tag, r.reason) == ("mac", "mac-workspace", "moved")
