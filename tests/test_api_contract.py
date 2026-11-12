import json
import subprocess
from pathlib import Path

import pytest

from conftest import HAS_FFMPEG, HAS_RESTIC
from home_services.contract import compatible, generate, shape

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "v1"


def test_public_health_has_no_detail_and_api_requires_token(env):
    c = env.client()
    assert c.get("/healthz").json() == {"status": "ok"}
    for path in ("/api/v1/bootstrap", "/api/v1/readiness", "/api/v1/capabilities", "/api/storage/contract"):
        r = c.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"


def test_contract_version_mismatch_is_actionable(env):
    r = env.client().get("/api/v1/bootstrap", headers={**env.token("storage:read"), "Home-Services-Contract": "2.0"})
    assert r.status_code == 400
    assert r.json()["error"]["details"]["supported_majors"] == [1]


def test_unsupported_is_never_empty_success(env):
    c = env.client()
    h = env.token("cameras:read", "cameras:control")
    r = c.post("/api/v1/cameras/workspace-pi-camera/live-sessions", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "capability_unavailable"
    caps = c.get("/api/v1/capabilities", headers=h).json()["capabilities"]
    assert caps["backups"]["retention_prune"]["state"] == "unavailable"
    assert caps["cameras"]["per_camera"]["workspace-pi-camera"]["recording"]["state"] == "unavailable"


def test_media_session_secret_not_logged(env, caplog):
    c = env.client()
    h = env.token("media:read")
    ev = c.get("/api/v1/media/legacy/events", headers=h).json()["events"][0]["id"]
    url = c.post("/api/v1/media/sessions", headers=h, json={"kind": "legacy_event_media", "event_id": ev, "name": "pre-00.jpg"}).json()["url"]
    with caplog.at_level("INFO", logger="home_services.access"):
        assert c.get(url).status_code == 200
    secret = url.rsplit("/", 1)[1]
    assert secret not in caplog.text and "<redacted>" in caplog.text


def test_upload_round_trip_over_http_with_chunks(env):
    import hashlib
    c = env.client()
    h = env.token("transfers:write", "storage:read")
    data = bytes(range(256)) * 400
    r = c.post("/api/v1/transfers/uploads", headers=h, json={"path": "Inbox/x.bin", "size": len(data),
                                                             "sha256": hashlib.sha256(data).hexdigest()})
    uid, off = r.json()["id"], 0
    for i in range(0, len(data), 30000):
        r = c.put(f"/api/v1/transfers/uploads/{uid}/content", headers={**h, "Upload-Offset": str(off)}, content=data[i:i + 30000])
        off = int(r.headers["Upload-Offset"])
    r = c.post(f"/api/v1/transfers/uploads/{uid}/complete", headers=h, json={"create_parents": True})
    assert r.json()["state"] == "published"
    assert c.get("/api/v1/storage/files/content", headers=h, params={"path": "Inbox/x.bin"}).content == data


def test_legacy_contract_accepts_predecessor_header(env):
    tok, _ = env.s.tokens.create("hp", ["legacy:storage-contract"])
    r = env.client().get("/api/storage/contract", headers={"X-Workspace-Token": tok})
    assert r.status_code == 200 and r.json()["mode"] == "pending"
    other, _ = env.s.tokens.create("x", ["storage:read"])
    assert env.client().get("/api/storage/contract", headers={"X-Workspace-Token": other}).status_code == 403


@pytest.fixture(scope="module")
def regenerated():
    if not HAS_RESTIC:
        pytest.skip("restic required to replay the committed backup examples")
    return generate(with_restic=True)


def test_committed_openapi_is_current(regenerated):
    _examples, openapi = regenerated
    committed = json.loads((CONTRACT / "openapi.json").read_text())
    assert committed == json.loads(json.dumps(openapi)), "run: home-services contract --out contracts/v1"


def test_committed_examples_match_live_behavior(regenerated):
    examples, _ = regenerated
    files = sorted((CONTRACT / "examples").glob("*.json"), key=lambda p: int(p.name.split("_", 1)[0]))
    assert len(files) == len(examples)
    for path, live in zip(files, examples):
        committed = json.loads(path.read_text())
        assert committed["name"] == live["name"], path.name
        assert committed["response"]["status"] == live["response"]["status"], path.name
        assert compatible(shape(committed["response"]["body"]), shape(live["response"]["body"])), path.name
        live_error = live["response"]["body"].get("error") if isinstance(live["response"]["body"], dict) else None
        if isinstance(live_error, dict) and "code" in live_error:  # error envelope, not a view's null `error` field
            assert committed["response"]["body"]["error"]["code"] == live_error["code"], path.name


def test_examples_contain_no_live_hostnames_or_real_tokens():
    text = "".join(p.read_text() for p in (CONTRACT / "examples").glob("*.json"))
    assert "pi.example.local" not in text and "tailef7acc" not in text and "/Users/example" not in text
    assert "fixture-secret-" in text  # only deterministic fixture credentials appear


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe required")
def test_gate_synthetic_media_ground_truth_and_report(tmp_path):
    from home_services.gate.runner import run_gate
    from home_services.gate.synthetic import ClipSpec
    spec = ClipSpec(width=320, height=180, fps=10, seconds=12, motion_start=4, motion_end=8)
    report = run_gate(tmp_path, spec=spec)
    steps = {s["step"]: s["status"] for s in report["steps"]}
    for name in ("tools", "synthetic_media", "ground_truth_motion", "rtsp_fixture", "no_device_access", "candidate_config"):
        assert steps[name] == "pass", (name, report)
    assert report["result"] == "not_passed" and report["smallest_failing_step"] == "engine_reachable"
    assert "/dev/" not in (tmp_path / "frigate-candidate.yml").read_text()


def test_resource_thresholds():
    from home_services.gate.fixtures import evaluate_samples
    good = [{"mem_available_bytes": 900_000_000, "load5": 2.5}]
    assert evaluate_samples(good)["passed"]
    assert not evaluate_samples(good + [{"mem_available_bytes": 400_000_000, "load5": 1.0}])["passed"]
    assert not evaluate_samples(good + [{"mem_available_bytes": 900_000_000, "load5": 4.0}])["passed"]


@pytest.mark.parametrize("script", sorted((ROOT / "deploy").glob("*.sh")))
def test_deploy_scripts_parse(script):
    subprocess.run(["bash", "-n", str(script)], check=True)
