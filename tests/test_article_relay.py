"""Isolated editorial transport: durable queues, scopes, replay, conflicts, removals."""
import json

import pytest

from home_services.articles import ArticleRelay
from home_services.auth import Principal

PATH = "/api/v1/articles/review"
WORKSPACE = "fixture-workspace"
ARTICLE = {"id": "draft-1", "title": "An article", "markdown": "Body", "revision": "r1", "review_digest": "d1", "workspace": {"complete": True}}


@pytest.fixture
def relay(env):
    c = env.client()
    sync = env.token("articles:read", "articles:sync")
    phone = env.token("articles:read", "articles:review")
    assert c.post(PATH, headers=sync, json={"action": "register", "workspace_id": WORKSPACE}).status_code == 200
    assert c.post(PATH, headers=sync, json={"action": "sync_snapshot", "workspace_id": WORKSPACE, "article": ARTICLE}).status_code == 200
    return c, sync, phone


def post(c, headers, action, **fields):
    return c.post(PATH, headers=headers, json={"action": action, "workspace_id": WORKSPACE, **fields})


def test_scopes_and_action_allowlist(relay, env):
    c, sync, phone = relay
    assert c.post(PATH, json={"action": "list"}).status_code == 401
    assert post(c, env.token("storage:read"), "list").status_code == 403
    assert post(c, phone, "pending").status_code == 403
    assert post(c, sync, "approve", article_id="draft-1", revision="r1", request_id="op", review_digest="d1").status_code == 403
    for action in ("release", "publish", "generate", "revoke", [], None):
        assert post(c, phone, action).status_code == 400
    assert post(c, phone, "save_article", article_id="draft-1", revision="r1", request_id="op", markdown="edit", cover_image="/etc/passwd").status_code == 400


def test_queue_replay_and_payload_collision(relay):
    c, sync, phone = relay
    args = dict(article_id="draft-1", revision="r1", request_id="op", markdown="My review")
    first = post(c, phone, "save_article", **args).json()
    assert first["status"] == "queued" and first["operation"]["state"] == "queued"
    assert post(c, phone, "save_article", **args).json()["operation"] == first["operation"]
    assert post(c, phone, "save_article", **{**args, "markdown": "Different request"}).status_code == 409
    pending = post(c, sync, "pending").json()["operations"]
    assert len(pending) == 1 and pending[0]["request"]["markdown"] == "My review"
    # Acceptance by the Pi does not mutate the article or approve anything.
    assert post(c, phone, "get", article_id="draft-1").json()["article"] == ARTICLE


def test_ack_updates_snapshot_and_is_idempotent(relay):
    c, sync, phone = relay
    post(c, phone, "approve", article_id="draft-1", revision="r1", request_id="op", review_digest="d1")
    applied = {**ARTICLE, "revision": "r2", "approval": "d1", "status": "ready to publish"}
    result = dict(request_id="op", state="applied", result={"status": "ok", "article": applied})
    receipt = post(c, sync, "resolve", **result).json()["operation"]
    assert receipt["state"] == "applied" and receipt["result"]["revision"] == "r2"
    assert not post(c, sync, "pending").json()["operations"]
    assert post(c, sync, "resolve", **result).json()["operation"] == receipt
    assert post(c, phone, "get", article_id="draft-1").json()["article"]["approval"] == "d1"


def test_stale_review_is_preserved_as_conflict(relay):
    c, sync, phone = relay
    r = post(c, phone, "save_article", article_id="draft-1", revision="old", request_id="op", markdown="Never discard me")
    assert r.json()["operation"]["state"] == "conflict"
    assert "preserved" in r.json()["operation"]["result"]["reason"]
    assert post(c, phone, "operation", request_id="op").json()["operation"]["state"] == "conflict"
    assert not post(c, sync, "pending").json()["operations"]


def test_persistence_survives_reopen(relay, env):
    c, _, phone = relay
    post(c, phone, "archive", article_id="draft-1", revision="r1", request_id="op")
    reopened = ArticleRelay(env.config.data_dir / "article-relay.sqlite3", env.clock, env.s.server_id)
    try:
        principal = Principal("sync", "Mac", {"articles:sync"})
        assert reopened.handle({"action": "pending", "workspace_id": WORKSPACE}, principal)["operations"][0]["request_id"] == "op"
    finally:
        reopened.db.close()


def test_workspace_mismatch_cannot_overwrite(relay):
    c, sync, phone = relay
    assert c.post(PATH, headers=sync, json={"action": "register", "workspace_id": "another-workspace"}).status_code == 409
    assert c.post(PATH, headers=phone, json={"action": "archive", "workspace_id": "another-workspace", "article_id": "draft-1", "revision": "r1", "request_id": "op"}).status_code == 409


def test_archive_rename_and_removed_snapshots_keep_receipts(relay):
    c, sync, phone = relay
    post(c, phone, "archive", article_id="draft-1", revision="r1", request_id="op")
    archived = {**ARTICLE, "id": "archived:draft-1", "revision": "r2", "declined": True}
    assert post(c, sync, "resolve", request_id="op", state="applied", result={"status": "ok", "article": archived}).status_code == 200
    assert post(c, phone, "get", article_id="draft-1").status_code == 404
    assert post(c, phone, "get", article_id="archived:draft-1").status_code == 200
    post(c, sync, "reconcile", article_ids=[])
    assert post(c, phone, "list").json()["articles"] == []
    assert post(c, phone, "operation", request_id="op").json()["operation"]["state"] == "applied"


def test_snapshot_bounds_and_private_fields(relay):
    c, sync, _ = relay
    for extra in ({"directory": "/private/workspace"}, {"metadata": {"secret": "evidence"}}):
        assert post(c, sync, "sync_snapshot", article={**ARTICLE, **extra}).status_code == 400
    response = c.post(PATH, headers=sync, content=b" " * 30_000_001)
    assert response.status_code == 413
    assert post(c, sync, "resolve", request_id="missing", state="applied", result={}).status_code == 404


def test_list_is_bounded_and_get_keeps_complete_body(relay):
    c, sync, phone = relay
    large = {**ARTICLE, "markdown": "a" * 6000, "mobile_assets": {"image.png": "image-data"}}
    post(c, sync, "sync_snapshot", article=large)
    listed = post(c, phone, "list").json()["articles"][0]
    assert len(listed["markdown"]) == 2048 and listed["body_complete"] is False
    assert "mobile_assets" not in listed
    assert post(c, phone, "get", article_id="draft-1").json()["article"] == large


def test_phone_media_is_bounded_and_dropped_after_resolution(relay, env):
    c, sync, phone = relay
    image = "A" * 3_000_000  # base64 text; the Mac validates names, hashes and image formats
    args = dict(article_id="draft-1", revision="r1", request_id="media-op", markdown="![](images/mobile-0123456789abcdef.jpg)",
                cover_image="images/mobile-0123456789abcdef.jpg", media={"images/mobile-0123456789abcdef.jpg": image})
    assert post(c, phone, "save_article", **args).json()["operation"]["state"] == "queued"
    assert post(c, phone, "save_article", **{**args, "markdown": "", "media": {}}).status_code == 400
    assert post(c, phone, "save_article", **{**args, "request_id": "too-big", "media": {"a": "A" * 17_000_000}}).status_code == 413
    assert post(c, phone, "save_article", **{**args, "request_id": "no-media-too-big", "media": None, "markdown": "x" * 2_100_000}).status_code in (400, 413)
    pending = post(c, sync, "pending").json()["operations"]
    assert pending[0]["request"]["media"]["images/mobile-0123456789abcdef.jpg"] == image
    applied = {**ARTICLE, "revision": "r2", "markdown": args["markdown"]}
    post(c, sync, "resolve", request_id="media-op", state="applied", result={"status": "ok", "article": applied})
    import sqlite3
    with sqlite3.connect(env.config.data_dir / "article-relay.sqlite3") as db:
        request, size = db.execute("SELECT request, request_bytes FROM reviews WHERE id='media-op'").fetchone()
    assert json.loads(request)["media"] == ["images/mobile-0123456789abcdef.jpg"] and size < 1000
    # The fingerprint still binds the original bytes: a retry with them is idempotent, different bytes conflict.
    assert post(c, phone, "save_article", **args).json()["operation"]["state"] == "applied"
    assert post(c, phone, "save_article", **{**args, "media": {"images/mobile-0123456789abcdef.jpg": "B"}}).status_code == 409


def test_pending_batches_are_bounded(relay):
    c, sync, phone = relay
    for index in range(3):
        post(c, phone, "save_article", article_id="draft-1", revision="r1", request_id=f"big-{index}", markdown=f"v{index}",
             media={"images/mobile-0123456789abcdef.jpg": "A" * 10_000_000})
    assert len(post(c, sync, "pending").json()["operations"]) == 2


def test_cover_paths_stay_inside_the_article(relay):
    c, sync, phone = relay
    for cover in ("../outside.png", "/abs.png", "images/../../x.png", ".hidden/x.png"):
        assert post(c, phone, "save_article", article_id="draft-1", revision="r1", request_id="c" + cover, cover_image=cover).status_code == 400
    assert post(c, phone, "save_article", article_id="draft-1", revision="r1", request_id="ok", cover_image="images/c.png").status_code == 200
