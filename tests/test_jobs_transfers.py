import hashlib

import pytest

from home_services.errors import Conflict, InsufficientSpace, VolumeUnavailable
from home_services.worker import Stop, work_once


def test_job_idempotency(env):
    j1, c1 = env.s.jobs.create("storage.cleanup", {}, idempotency_key="k")
    j2, c2 = env.s.jobs.create("storage.cleanup", {}, idempotency_key="k")
    assert c1 and not c2 and j1["id"] == j2["id"]
    env.s.jobs.create("backup.restore", {"snapshot_id": "a" * 8, "include": ["/x"]}, idempotency_key="r")
    with pytest.raises(Conflict, match="different request"):
        env.s.jobs.create("backup.restore", {"snapshot_id": "b" * 8, "include": ["/x"]}, idempotency_key="r")


def test_expired_lease_becomes_interrupted_not_success(env):
    j, _ = env.s.jobs.create("storage.cleanup", {}, idempotency_key="k")
    claimed = env.s.jobs.claim("storage", "w1", lease_seconds=60)
    assert claimed["state"] == "running"
    env.clock.advance(61)
    assert env.s.jobs.reap_expired() == [j["id"]]
    job = env.s.jobs.get(j["id"])
    assert job["state"] == "interrupted" and job["error"]["code"] == "lease_expired"
    with pytest.raises(Conflict):  # the stale worker cannot report afterwards
        env.s.jobs.finish(j["id"], "w1", "succeeded")


def test_heartbeat_and_progress_are_separate(env):
    j, _ = env.s.jobs.create("storage.cleanup", {}, idempotency_key="k")
    env.s.jobs.claim("storage", "w1", 60)
    env.clock.advance(10)
    env.s.jobs.heartbeat(j["id"], "w1", 60, {"done": 1})
    env.clock.advance(20)
    env.s.jobs.heartbeat(j["id"], "w1", 60)
    job = env.s.jobs.get(j["id"])
    assert job["heartbeat"]["age_seconds"] == 0 and job["progress"]["age_seconds"] == 20


def test_cancel_waiting_job_and_running_job_flag(env):
    j, _ = env.s.jobs.create("storage.cleanup", {}, idempotency_key="a")
    assert env.s.jobs.request_cancel(j["id"])["state"] == "cancelled"
    j2, _ = env.s.jobs.create("storage.cleanup", {}, idempotency_key="b")
    env.s.jobs.claim("storage", "w", 60)
    assert env.s.jobs.request_cancel(j2["id"])["cancel_requested"] is True
    assert env.s.jobs.heartbeat(j2["id"], "w", 60) is True


def test_cleanup_job_blocks_when_volume_missing_then_retries(env):
    j, _ = env.s.jobs.create("storage.cleanup", {}, idempotency_key="k")
    env.unplug()
    assert work_once(env.s, "storage", "w", Stop())
    job = env.s.jobs.get(j["id"])
    assert job["state"] == "blocked" and job["error"]["code"] == "volume_unavailable"
    assert env.s.jobs.claim("storage", "w", 60) is None  # parked until retry_at
    env.replug()
    env.clock.advance(61)
    assert work_once(env.s, "storage", "w", Stop())
    assert env.s.jobs.get(j["id"])["state"] == "succeeded"


def _upload(env, data: bytes, path="Documents/a.txt", **kw):
    view, _ = env.s.transfers.create(root="files", path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                     conflict=kw.get("conflict", "keep_both"), idempotency_key=kw.get("key"), principal_id=None)
    return view


def test_resume_truncates_unacknowledged_bytes(env):
    data = b"0123456789" * 10
    up = _upload(env, data)
    env.s.transfers.append(up["id"], 0, [data[:40]])
    partial = env.mount / "home-services/staging/uploads" / f"{up['id']}.part"
    with open(partial, "ab") as f:
        f.write(b"GARBAGE")  # crash after write, before commit
    with pytest.raises(Conflict) as e:
        env.s.transfers.append(up["id"], 47, [b"x"])
    assert e.value.details["expected_offset"] == 40
    env.s.transfers.append(up["id"], 40, [data[40:]])
    done = env.s.transfers.complete(up["id"], create_parents=True)
    assert done["state"] == "published"
    assert (env.files / "Documents/a.txt").read_bytes() == data


def test_checksum_mismatch_never_publishes(env):
    up = _upload(env, b"expected")
    env.s.transfers.append(up["id"], 0, [b"tampered"])
    with pytest.raises(Conflict, match="SHA-256"):
        env.s.transfers.complete(up["id"], create_parents=True)
    assert not (env.files / "Documents/a.txt").exists()


def test_conflict_fail_and_keep_both(env):
    (env.files / "Documents").mkdir()
    (env.files / "Documents/a.txt").write_text("original")
    up = _upload(env, b"new", conflict="fail")
    env.s.transfers.append(up["id"], 0, [b"new"])
    with pytest.raises(Conflict, match="destination exists"):
        env.s.transfers.complete(up["id"])
    assert (env.files / "Documents/a.txt").read_text() == "original"
    up2 = _upload(env, b"new2")
    env.s.transfers.append(up2["id"], 0, [b"new2"])
    assert env.s.transfers.complete(up2["id"])["published_path"] == "Documents/a (1).txt"


def test_size_and_space_limits(env):
    up = _upload(env, b"abc")
    with pytest.raises(Exception, match="declared size"):
        env.s.transfers.append(up["id"], 0, [b"abcd"])
    with pytest.raises(InsufficientSpace):  # fixture staging cap is 2 GB
        env.s.transfers.create(root="files", path="big.bin", size=3_000_000_000, sha256=None, conflict="keep_both",
                               idempotency_key=None, principal_id=None)


def test_volume_loss_mid_upload_fails_closed(env):
    up = _upload(env, b"abcdef")
    env.s.transfers.append(up["id"], 0, [b"abc"])
    env.unplug()
    with pytest.raises(VolumeUnavailable):
        env.s.transfers.append(up["id"], 3, [b"def"])
    env.replug()
    assert env.s.transfers.view(up["id"])["offset"] == 3


def test_abandoned_sessions_cleaned_after_24h(env):
    up = _upload(env, b"abc")
    env.clock.advance(24 * 3600 + 1)
    assert env.s.transfers.cleanup_abandoned() == {"abandoned_sessions": 1, "removed_partials": 1}
    assert env.s.transfers.view(up["id"])["state"] == "abandoned"


def test_trash_restore_and_purge(env):
    (env.files / "f.txt").write_text("x")
    item = env.s.storage.trash("f.txt", None)
    assert not (env.files / "f.txt").exists()
    (env.files / "f.txt").write_text("replacement")
    assert env.s.storage.restore(item["id"]) == {"restored_path": "f (1).txt", "renamed": True}
    item2 = env.s.storage.trash("f.txt", None)
    env.clock.advance(7 * 86400 + 1)
    assert env.s.storage.purge_expired_trash() == 1
    assert env.s.storage.list_trash() == []
    assert not (env.mount / "home-services/staging/trash" / item2["id"]).exists()


def test_restore_is_not_succeeded_when_the_volume_vanishes_as_restic_finishes(env, monkeypatch):
    j, _ = env.s.jobs.create("backup.restore", {"snapshot_id": "ab" * 32, "include": ["/x"]}, idempotency_key="k")

    def fake_restore(*_a, **_kw):
        env.unplug()  # the disk disappears between restic's last write and the worker's report
        return {"message_type": "summary", "total_bytes": 10, "bytes_restored": 10}
    monkeypatch.setattr(env.s.restic, "restore", fake_restore)
    work_once(env.s, "backup", "w", Stop())
    job = env.s.jobs.get(j["id"])
    assert job["state"] == "failed" and job["error"]["code"] == "volume_unavailable"


def test_restore_with_missing_bytes_is_failed(env, monkeypatch):
    j, _ = env.s.jobs.create("backup.restore", {"snapshot_id": "ab" * 32, "include": ["/x"]}, idempotency_key="k")
    monkeypatch.setattr(env.s.restic, "restore", lambda *_a, **_kw: {"total_bytes": 10, "bytes_restored": 7})
    work_once(env.s, "backup", "w", Stop())
    job = env.s.jobs.get(j["id"])
    assert job["state"] == "failed" and job["error"]["code"] == "restore_incomplete"
