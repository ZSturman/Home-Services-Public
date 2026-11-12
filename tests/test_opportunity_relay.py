"""Isolated Opportunity Discovery relay: scopes, path allowlist, replay, chaining, receipts."""
import pytest

from home_services.auth import Principal
from home_services.opportunities import OpportunityRelay

PATH = "/api/v1/opportunities/relay"
WORKSPACE = "fixture-workspace"
CASE_KEY = "/api/v2/review/case-1"
CASE = {"id": "case-1", "revision": 3, "stage": "Awaiting Review", "decision": ""}


@pytest.fixture
def relay(env):
    c = env.client()
    sync = env.token("opportunities:read", "opportunities:sync")
    phone = env.token("opportunities:read", "opportunities:review")
    assert post(c, sync, "register").status_code == 200
    assert post(c, sync, "sync", resources={CASE_KEY: CASE, "/api/mobile/summary": {"to_review": 1}}).status_code == 200
    return c, sync, phone


def post(c, headers, action, **fields):
    return c.post(PATH, headers=headers, json={"action": action, "workspace_id": WORKSPACE, **fields})


def submit(c, phone, request_id, path=CASE_KEY + "/decision", body=None, **extra):
    return post(c, phone, "submit", request_id=request_id, path=path, body=body or {"revision": 3, "decision": "approve"}, **extra)


def test_scopes_and_sending_is_never_queued(relay, env):
    c, sync, phone = relay
    assert c.post(PATH, json={"action": "get", "key": CASE_KEY}).status_code == 401
    assert post(c, env.token("articles:read", "articles:review"), "get", key=CASE_KEY).status_code == 403
    assert post(c, phone, "pending").status_code == 403
    assert post(c, phone, "sync", resources={CASE_KEY: {}}).status_code == 403
    assert submit(c, sync, "op").status_code == 403
    for path in ("/api/v2/outbox/dispatch", "/api/v2/outbox/test", "/api/v2/review/case-1/manual-contact",
                 "/api/v2/review/case-1/stage", "/api/v2/review/../outbox/dispatch", "/api/v2/outbox/abc/send"):
        assert submit(c, phone, "op-" + path, path=path).status_code == 400
    for action in ("send", "dispatch", None, []):
        assert post(c, phone, action).status_code == 400


def test_read_resources_by_phone_path(relay):
    c, _, phone = relay
    found = post(c, phone, "get", keys=[CASE_KEY, "/api/mobile/summary", "/api/v2/outbox"]).json()
    assert found["resources"][CASE_KEY]["value"] == CASE
    assert "/api/v2/outbox" not in found["resources"]
    assert found["workspace_id"] == WORKSPACE and found["companion_seen_at"]
    assert post(c, phone, "get", key="/etc/passwd").status_code == 400


def test_register_reports_digests_and_sync_removes(relay):
    c, sync, _ = relay
    digests = post(c, sync, "register").json()["digests"]
    assert set(digests) == {CASE_KEY, "/api/mobile/summary"}
    post(c, sync, "sync", remove=["/api/mobile/summary"])
    assert set(post(c, sync, "register").json()["digests"]) == {CASE_KEY}


def test_queue_replay_collision_and_stale_revision(relay):
    c, sync, phone = relay
    first = submit(c, phone, "op").json()
    assert first["status"] == "queued" and first["operation"]["state"] == "queued" and first["operation"]["case_id"] == "case-1"
    assert submit(c, phone, "op").json()["operation"] == first["operation"]
    assert submit(c, phone, "op", body={"revision": 3, "decision": "reject"}).status_code == 409
    stale = submit(c, phone, "stale", body={"revision": 2, "decision": "reject"}).json()["operation"]
    assert stale["state"] == "conflict" and "preserved" in stale["result"]["reason"]
    pending = post(c, sync, "pending").json()["operations"]
    assert [p["request_id"] for p in pending] == ["op"]
    assert pending[0]["request"] == {"path": CASE_KEY + "/decision", "body": {"revision": 3, "decision": "approve"}, "after": None}
    # Queuing changes nothing the phone reads.
    assert post(c, phone, "get", key=CASE_KEY).json()["resources"][CASE_KEY]["value"] == CASE


def test_chained_steps_wait_for_the_mac(relay):
    c, sync, phone = relay
    submit(c, phone, "decide")
    chained = submit(c, phone, "notes", path=CASE_KEY + "/notes", body={"revision": 3, "notes": "x"}, after="decide").json()
    assert chained["operation"]["state"] == "queued" and chained["operation"]["after"] == "decide"
    assert submit(c, phone, "orphan", path=CASE_KEY + "/notes", body={"revision": 3, "notes": "y"}, after="missing").status_code == 400
    schedule = submit(c, phone, "schedule", path="/api/v2/outbox/schedule",
                      body={"case_id": "case-1", "revision": 3, "confirmed": True, "recipient_confirmed": True}, after="notes").json()
    assert schedule["operation"]["case_id"] == "case-1"
    assert [p["after"] for p in post(c, sync, "pending").json()["operations"]] == [None, "decide", "notes"]


def test_resolve_updates_copies_with_the_receipt(relay):
    c, sync, phone = relay
    submit(c, phone, "op")
    applied = {**CASE, "revision": 4, "stage": "Approved", "decision": "approve"}
    result = dict(request_id="op", state="applied", result={"status": "ok", "case_revision": 4, "response": applied},
                  resources={CASE_KEY: applied})
    receipt = post(c, sync, "resolve", **result).json()["operation"]
    assert receipt["state"] == "applied" and receipt["result"]["case_revision"] == 4
    assert post(c, phone, "get", key=CASE_KEY).json()["resources"][CASE_KEY]["value"] == applied
    # Terminal outcomes do not change.
    assert post(c, sync, "resolve", request_id="op", state="conflict", result={"status": "refused", "reason": "late"}).json()["operation"] == receipt
    assert post(c, phone, "operations", request_ids=["op", "unknown"]).json()["operations"] == [receipt]
    assert not post(c, sync, "pending").json()["operations"]
    submit(c, phone, "bad", body={"revision": 4, "decision": "reject"})
    assert post(c, sync, "resolve", request_id="bad", state="applied", result={"status": "error"}).status_code == 400
    assert post(c, sync, "resolve", request_id="bad", state="conflict", result={"status": "refused"}).status_code == 400


def test_workspace_mismatch_and_bounds(relay):
    c, sync, phone = relay
    assert c.post(PATH, headers=sync, json={"action": "register", "workspace_id": "other"}).status_code == 409
    assert c.post(PATH, headers=phone, json={"action": "submit", "workspace_id": "other", "request_id": "x",
                                              "path": CASE_KEY + "/notes", "body": {}}).status_code == 409
    assert post(c, sync, "sync", resources={"/api/mobile/huge": "a" * 2_000_001}).status_code == 413
    assert post(c, sync, "sync", resources={"not-a-path": {}}).status_code == 400
    assert submit(c, phone, "big", path=CASE_KEY + "/draft", body={"revision": 3, "body": "x" * 1_000_001}).status_code == 413
    assert submit(c, phone, "extra", unexpected=True).status_code == 400
    assert c.post(PATH, headers=sync, content=b" " * 25_000_001).status_code == 413


def test_persistence_survives_reopen(relay, env):
    c, _, phone = relay
    submit(c, phone, "op")
    reopened = OpportunityRelay(env.config.data_dir / "opportunity-relay.sqlite3", env.clock, env.s.server_id)
    try:
        principal = Principal("sync", "Mac", {"opportunities:sync"})
        assert reopened.handle({"action": "pending", "workspace_id": WORKSPACE}, principal)["operations"][0]["request_id"] == "op"
    finally:
        reopened.db.close()
