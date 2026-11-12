"""Chew Sense mailbox: phone deposits, Mac collection, deletion after acknowledgement."""
import hashlib

from home_services import chewsense

BASE = "/api/v1/chewsense"
SEALED = bytes(range(256)) * 400  # stands in for an encrypted archive; never parsed here
SHA = hashlib.sha256(SEALED).hexdigest()
ARCHIVE = "a" * 64


def announce(c, phone, data=SEALED, session="Guided-test-20990101-000000-AAAAAAAA"):
    return c.post(f"{BASE}/deposits", headers=phone, json={
        "session_id": session, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "archive_sha256": ARCHIVE, "device": "Test iPhone"})


def put(c, phone, deposit_id, offset, data):
    return c.put(f"{BASE}/deposits/{deposit_id}/content", headers={**phone, "Upload-Offset": str(offset)}, content=data)


def deposit(c, phone, data=SEALED):
    view = announce(c, phone, data).json()
    assert put(c, phone, view["id"], 0, data).status_code == 200
    assert c.post(f"{BASE}/deposits/{view['id']}/complete", headers=phone).json()["state"] == "waiting"
    return view["id"]


def test_scopes_keep_video_away_from_the_phone_token(env):
    c = env.client()
    phone, mac = env.token("chewsense:deposit"), env.token("chewsense:collect")
    assert c.get(f"{BASE}/pending").status_code == 401
    assert announce(c, env.token("storage:read")).status_code == 403
    assert announce(c, mac).status_code == 403
    deposit_id = deposit(c, phone)
    assert c.get(f"{BASE}/pending", headers=phone).status_code == 403
    assert c.get(f"{BASE}/deposits/{deposit_id}/content", headers=phone).status_code == 403
    assert c.post(f"{BASE}/deposits/{deposit_id}/ack", headers=phone, json={"result": "imported"}).status_code == 403
    assert c.put(f"{BASE}/status", headers=phone, json={"x": 1}).status_code == 403
    assert c.get(f"{BASE}/deposits/{deposit_id}", headers=mac).json()["state"] == "waiting"
    response = c.get(f"{BASE}/deposits/{deposit_id}", headers=phone)
    assert response.headers["Cache-Control"] == "no-store"


def test_interrupted_deposit_resumes_and_mac_collects_then_copy_is_deleted(env):
    c = env.client()
    phone, mac = env.token("chewsense:deposit"), env.token("chewsense:collect")
    view = announce(c, phone).json()
    half = len(SEALED) // 2
    assert put(c, phone, view["id"], 0, SEALED[:half]).json()["offset"] == half
    # Connection lost; the phone announces again and learns where to continue.
    again = announce(c, phone)
    assert again.status_code == 200 and again.json()["id"] == view["id"] and again.json()["offset"] == half
    wrong = put(c, phone, view["id"], 0, SEALED[:10])
    assert wrong.status_code == 409 and wrong.json()["error"]["details"]["expected_offset"] == half
    assert c.post(f"{BASE}/deposits/{view['id']}/complete", headers=phone).json()["error"]["code"] == "deposit_incomplete"
    put(c, phone, view["id"], half, SEALED[half:])
    assert c.post(f"{BASE}/deposits/{view['id']}/complete", headers=phone).json()["state"] == "waiting"

    pending = c.get(f"{BASE}/pending", headers=mac).json()["deposits"]
    assert [d["id"] for d in pending] == [view["id"]] and pending[0]["archive_sha256"] == ARCHIVE
    ranged = c.get(f"{BASE}/deposits/{view['id']}/content", headers={**mac, "Range": f"bytes={half}-"})
    assert ranged.status_code == 206 and ranged.content == SEALED[half:]
    assert c.get(f"{BASE}/deposits/{view['id']}/content", headers=mac).content == SEALED

    ack = c.post(f"{BASE}/deposits/{view['id']}/ack", headers=mac,
                 json={"result": "imported", "session_id": view["session_id"]}).json()
    assert ack["state"] == "collected" and ack["receipt"]["result"] == "imported"
    assert not list((env.mount / "home-services").rglob(f"{view['id']}.sealed"))
    assert c.get(f"{BASE}/pending", headers=mac).json()["deposits"] == []
    assert c.post(f"{BASE}/deposits/{view['id']}/ack", headers=mac, json={"result": "rejected"}).json()["state"] == "collected"
    assert c.get(f"{BASE}/deposits/{view['id']}", headers=phone).json()["receipt"]["result"] == "imported"
    # The same sealed archive again is the same deposit, never a second copy.
    assert announce(c, phone).json()["id"] == view["id"]


def test_corrupted_deposit_restarts_and_size_limits_hold(env):
    c = env.client()
    phone = env.token("chewsense:deposit")
    view = announce(c, phone).json()
    put(c, phone, view["id"], 0, b"x" * len(SEALED))
    bad = c.post(f"{BASE}/deposits/{view['id']}/complete", headers=phone)
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "checksum_mismatch"
    assert c.get(f"{BASE}/deposits/{view['id']}", headers=phone).json()["offset"] == 0
    assert put(c, phone, view["id"], 0, SEALED + b"extra").status_code == 413
    assert c.post(f"{BASE}/deposits", headers=phone, json={"session_id": "s", "size": chewsense.MAX_DEPOSIT + 1,
                                                          "sha256": SHA, "archive_sha256": ARCHIVE}).status_code == 413
    assert c.post(f"{BASE}/deposits", headers=phone, json={"session_id": "../x", "size": 5,
                                                          "sha256": SHA, "archive_sha256": ARCHIVE}).status_code == 400


def test_quota_and_missing_disk_fail_closed(env, monkeypatch):
    c = env.client()
    phone = env.token("chewsense:deposit")
    monkeypatch.setattr(chewsense, "MAX_HELD", len(SEALED) + 10)
    deposit(c, phone)
    other = SEALED[::-1]
    full = announce(c, phone, other, session="Guided-test-20990101-000001-BBBBBBBB")
    assert full.status_code == 507
    monkeypatch.setattr(chewsense, "MAX_HELD", 10**12)
    env.unplug()
    assert announce(c, phone, other, session="Guided-test-20990101-000001-BBBBBBBB").status_code == 503
    env.replug()
    assert announce(c, phone, other, session="Guided-test-20990101-000001-BBBBBBBB").status_code == 201


def test_cancel_and_abandon_remove_partial_copies(env):
    c = env.client()
    phone = env.token("chewsense:deposit")
    first = announce(c, phone).json()
    put(c, phone, first["id"], 0, SEALED[:100])
    assert c.delete(f"{BASE}/deposits/{first['id']}", headers=phone).json()["state"] == "cancelled"
    other = SEALED[::-1]
    second = announce(c, phone, other, session="Guided-test-20990101-000001-BBBBBBBB").json()
    env.clock.advance(chewsense.ABANDON_SECONDS + 1)
    announce(c, phone, SEALED[1:], session="Guided-test-20990101-000002-CCCCCCCC")
    assert c.get(f"{BASE}/deposits/{second['id']}", headers=phone).json()["state"] == "abandoned"
    assert not list((env.mount / "home-services").rglob(f"{second['id']}.sealed"))


def test_status_copy_from_mac_to_phone(env):
    c = env.client()
    phone, mac = env.token("chewsense:deposit"), env.token("chewsense:collect")
    assert c.get(f"{BASE}/status", headers=phone).status_code == 404
    assert c.put(f"{BASE}/status", headers=mac, json={"status": {"headline": "Label"}, "sessions": {}}).status_code == 200
    copy = c.get(f"{BASE}/status", headers=phone).json()
    assert copy["status"]["status"]["headline"] == "Label" and copy["updated_at"]
    assert c.put(f"{BASE}/status", headers=mac, json={"x": "y" * 1_000_001}).status_code == 413
