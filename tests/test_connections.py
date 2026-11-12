import base64
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from home_services.auth import PRESETS
from home_services.cli import main
from home_services.connections import Connections
from home_services.errors import ServiceError


def invitation(env, preset="home-page", scopes=None):
    return Connections(env.s).invite(preset=preset, name="Fixture app", url="https://host.example.net:8443", scopes=scopes)


def claim(invite, **extra):
    return {k: invite["invitation"][k] for k in ("invitation_id", "secret")} | {
        "client_secret": "a" * 43, "device_name": "Fixture phone", **extra}


def pair(env, preset="home-page"):
    c = env.client()
    invite = invitation(env, preset)
    r = c.post("/api/v1/pairing/exchange", json=claim(invite))
    assert r.status_code == 200, r.text
    body = r.json()
    return c, body, {"Authorization": "Bearer " + body["token"]}


def test_qr_payload_has_expiring_invitation_and_never_a_bearer_token(env):
    invite = invitation(env)
    fragment = invite["pairing_uri"].split("#")[1]
    payload = json.loads(base64.urlsafe_b64decode(fragment + "=" * (-len(fragment) % 4)))
    assert payload == invite["invitation"]
    assert payload["protocol"] == 1
    assert "hst_" not in json.dumps(invite)
    assert env.s.tokens.list() == []
    stored = env.s.db.one("SELECT * FROM connection_invitations")
    assert payload["secret"] not in str(dict(stored))


def test_concurrent_first_connections_get_one_stable_server_identity(env):
    from home_services.services import build_services
    other = build_services(env.config, clock=env.clock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            identities = list(pool.map(lambda services: services.server_id, [env.s, other]))
        assert identities[0] == identities[1] == env.s.server_id
    finally:
        other.db.close()


def test_preview_then_exchange_then_existing_api_and_self_access(env, caplog):
    invite = invitation(env)
    c = env.client()
    with caplog.at_level("INFO", logger="home_services.access"):
        preview = c.post("/api/v1/pairing/preview", json={k: claim(invite)[k] for k in ("invitation_id", "secret")})
        result = c.post("/api/v1/pairing/exchange", json=claim(invite))
    assert preview.status_code == result.status_code == 200
    assert preview.json()["server_id"] == result.json()["server_id"]
    assert preview.headers["cache-control"] == result.headers["cache-control"] == "no-store"
    h = {"Authorization": "Bearer " + result.json()["token"]}
    assert c.get("/api/v1/readiness", headers=h).status_code == 200
    assert c.get("/api/v1/storage/volumes", headers=h).status_code == 403
    assert c.get("/api/v1/connection", headers=h).json()["token"]["scopes"] == ["status:read"]
    assert invite["invitation"]["secret"] not in caplog.text
    assert claim(invite)["client_secret"] not in caplog.text
    assert result.json()["token"] not in caplog.text
    assert "secret_hash" not in c.get("/api/v1/connection", headers=h).text


def test_lost_response_replay_recovers_only_original_exchange(env):
    invite = invitation(env)
    c = env.client()
    first = c.post("/api/v1/pairing/exchange", json=claim(invite)).json()
    assert c.post("/api/v1/pairing/exchange", json=claim(invite)).json() == first
    assert len(env.s.tokens.list()) == 1
    for extra in ({"client_secret": "b" * 43}, {"device_name": "Other phone"}):
        r = c.post("/api/v1/pairing/exchange", json=claim(invite, **extra))
        assert r.status_code == 409 and r.json()["error"]["code"] == "pairing_used"
    env.s.tokens.revoke(first["connection"]["token"]["id"])
    assert c.post("/api/v1/pairing/exchange", json=claim(invite)).status_code == 401


def test_only_one_concurrent_claim_wins(env):
    from home_services.services import build_services
    invite = invitation(env)
    other = build_services(env.config, clock=env.clock)
    stores = [Connections(env.s), Connections(other)]

    def exchange(args):
        store, secret = args
        data = claim(invite, client_secret=secret)
        try:
            return store.exchange(**data)
        except ServiceError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(exchange, zip(stores, ["a" * 43, "b" * 43])))
    finally:
        other.db.close()
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "pairing_used" in results
    assert len(env.s.tokens.list()) == 1


def test_invitation_expiry_cancel_and_invalid_secret(env):
    c = env.client()
    invite = invitation(env)
    assert c.post("/api/v1/pairing/exchange", json=claim(invite, secret="wrong-secret-value")).status_code == 401
    env.clock.advance(300)
    assert c.post("/api/v1/pairing/exchange", json=claim(invite)).json()["error"]["code"] == "pairing_expired"
    invite = invitation(env)
    Connections(env.s).cancel(invite["invitation"]["invitation_id"])
    assert c.post("/api/v1/pairing/exchange", json=claim(invite)).status_code == 401
    assert env.s.tokens.list() == []


def test_toggle_permissions_bounded_by_original_approval_and_revoke(env):
    c, paired, h = pair(env, "content-manager")
    conn = paired["connection"]
    r = c.patch("/api/v1/connection", headers=h, json={"scopes": ["storage:write"]})
    assert r.status_code == 403
    assert c.patch("/api/v1/connection", headers=h, json={"scopes": []}).status_code == 200
    assert c.post("/api/v1/articles/review", headers=h, json={"action": "status"}).status_code == 403
    assert c.patch("/api/v1/connection", headers=h, json={"scopes": ["articles:read"]}).status_code == 200
    assert env.s.tokens.authenticate(paired["token"]).scopes == {"articles:read"}
    assert Connections(env.s).for_token(conn["token"]["id"])["allowed_scopes"] == PRESETS["content-manager"]
    assert c.delete("/api/v1/connection", headers=h).status_code == 200
    assert c.get("/api/v1/connection", headers=h).status_code == 401


def test_preferences_are_validated_against_guarded_storage_and_configured_cameras(env):
    c, _, h = pair(env, "files-backups")
    (env.files / "Inbox").mkdir()
    for folder in ("Inbox", ""):
        r = c.patch("/api/v1/connection", headers=h, json={"preferences": {"upload_folder": folder}})
        assert r.status_code == 200, r.text
    for prefs in ({"upload_folder": "../staging"}, {"upload_folder": "/tmp"}, {"upload_folder": "missing"},
                  {"camera_id": "nope"}, {"unknown": True}):
        assert c.patch("/api/v1/connection", headers=h, json={"preferences": prefs}).status_code >= 400
    env.unplug()
    assert c.patch("/api/v1/connection", headers=h, json={"preferences": {"upload_folder": "Inbox"}}).status_code == 503
    env.replug()
    cc, _, ch = pair(env, "camera-monitor")
    camera_id = env.s.config.cameras.devices[0].id
    assert cc.patch("/api/v1/connection", headers=ch, json={"preferences": {"camera_id": camera_id}}).status_code == 200
    assert cc.patch("/api/v1/connection", headers=ch, json={"preferences": {"upload_folder": ""}}).status_code == 403
    for camera in ([], {}, 42):
        assert cc.patch("/api/v1/connection", headers=ch, json={"preferences": {"camera_id": camera}}).status_code == 400


def test_adopt_existing_credentials_without_rotation_or_broadening(env):
    token, info = env.s.tokens.create("Already connected", PRESETS["home-page"])
    store = Connections(env.s)
    adopted = store.adopt(info["id"], "home-page")
    assert adopted["token"] == info
    assert env.s.tokens.authenticate(token).token_id == info["id"]
    assert store.list()["existing_tokens"] == []
    with pytest.raises(ServiceError):
        store.update(adopted["id"], scopes=["storage:write"])
    admin, info = env.s.tokens.create("Admin", PRESETS["admin"])
    with pytest.raises(ServiceError):
        store.adopt(info["id"], "files-backups")
    assert env.s.tokens.authenticate(admin)
    _, expired = env.s.tokens.create("Expired", PRESETS["home-page"], ttl_days=1)
    env.clock.advance(86400)
    with pytest.raises(ServiceError):
        store.adopt(expired["id"], "home-page")


@pytest.mark.parametrize("url", ["http://home.example", "https://user:pass@home.example", "https://home.example/path",
                                  "https://home.example?q=secret", "https://home.example#secret", "https://home.example:bad", "https:// home.example", "https://@home.example"])
def test_unsafe_gateway_urls_refused(env, url):
    with pytest.raises(ServiceError):
        Connections(env.s).invite(preset="home-page", name="Test", url=url)


def test_invitation_cannot_request_admin_or_exceed_preset(env):
    with pytest.raises(ServiceError):
        invitation(env, "admin")
    with pytest.raises(ServiceError):
        invitation(env, scopes=["storage:write"])
    with pytest.raises(ServiceError):
        invitation(env, scopes=[])


def test_connection_data_private_and_validation_does_not_echo_secrets(env):
    c, _, h = pair(env)
    assert c.get("/api/v1/connection").status_code == 401
    r = c.post("/api/v1/pairing/exchange", json={"invitation_id": "x", "secret": "private", "client_secret": "bad-secret", "device_name": ""})
    assert r.status_code == 422
    assert "private" not in r.text and "bad-secret" not in r.text
    old, _ = env.s.tokens.create("Old app", ["status:read"])
    assert c.get("/api/v1/connection", headers={"Authorization": "Bearer " + old}).status_code == 404
    assert c.get("/api/v1/readiness", headers={"Authorization": "Bearer " + old}).status_code == 200


def test_cli_invite_list_adopt_update_and_revoke(env, capsys):
    prefix = ["--config", str(env.config_path), "connections"]
    assert main(prefix + ["invite", "--name", "Home Page", "--preset", "home-page", "--base-url", "https://host.example.net:8443"]) == 0
    invite = json.loads(capsys.readouterr().out)
    assert "homeservices://pair#" in invite["pairing_uri"]
    assert main(prefix + ["list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert "secret" not in json.dumps(listing)
    assert main(prefix + ["cancel", invite["invitation"]["invitation_id"]]) == 0
    capsys.readouterr()
    _, info = env.s.tokens.create("Existing", ["status:read"])
    assert main(prefix + ["adopt", info["id"], "--preset", "home-page"]) == 0
    conn = json.loads(capsys.readouterr().out)
    assert main(prefix + ["update", conn["id"], "--disable-all"]) == 0
    assert json.loads(capsys.readouterr().out)["token"]["scopes"] == []
    assert main(prefix + ["revoke", conn["id"]]) == 0
