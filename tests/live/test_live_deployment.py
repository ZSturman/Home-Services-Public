"""Acceptance tests against a deployed Home Services host.

Skipped unless HOME_SERVICES_LIVE_URL and HOME_SERVICES_LIVE_TOKEN (an admin
token) are set. HOME_SERVICES_LIVE_SSH (e.g. service-user@pi.example.local) enables the
checks that need the host itself: creating and revoking scoped tokens,
hashing originals, and comparing with the predecessor's contract.

Everything that writes stays inside files/_acceptance/<run id>/ and ends in
managed Trash (purged after the retention window). Restores land in fresh
files/Restores/<job id>/ directories. Nothing touches the repository,
legacy camera media, or anything outside the Home Services roots.

    HOME_SERVICES_LIVE_URL=https://pi.example.local.<tailnet>.ts.net:8443 \
    HOME_SERVICES_LIVE_TOKEN=$(security find-generic-password -s home-services-admin-token -w) \
    HOME_SERVICES_LIVE_SSH=service-user@pi.example.local .venv/bin/python -m pytest tests/live -v
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import secrets
import shlex
import socket
import ssl
import subprocess
import time
import urllib.parse

import httpx
import pytest

URL = os.environ.get("HOME_SERVICES_LIVE_URL", "").rstrip("/")
TOKEN = os.environ.get("HOME_SERVICES_LIVE_TOKEN", "")
SSH = os.environ.get("HOME_SERVICES_LIVE_SSH", "")
CLI = "/opt/home-services/current/venv/bin/home-services --config /etc/home-services/config.toml"

pytestmark = pytest.mark.skipif(not (URL and TOKEN), reason="live deployment not configured")
needs_ssh = pytest.mark.skipif(not SSH, reason="HOME_SERVICES_LIVE_SSH not set")
CHUNK = 4 * 1024 * 1024


def ssh(command: str, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", SSH, command], capture_output=True, text=True, check=check, timeout=300)


def make_token(preset: str, ttl_days: float | None = None) -> tuple[str, str]:
    ttl = f" --ttl-days {ttl_days}" if ttl_days else ""
    out = ssh(f"{CLI} tokens create --name 'live acceptance {preset}' --preset {preset}{ttl}")
    token = out.stderr.strip().rsplit(" ", 1)[-1]
    return token, json.loads(out.stdout)["id"]


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=URL, headers={"Authorization": f"Bearer {TOKEN}", "Home-Services-Contract": "1"}, timeout=120) as c:
        yield c


@pytest.fixture(scope="module")
def run_dir(api):
    rel = f"_acceptance/{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
    for p in ("_acceptance", rel):
        r = api.post("/api/v1/storage/files/directories", json={"path": p})
        assert r.status_code in (201, 409), r.text
    yield rel
    r = api.post("/api/v1/storage/files/trash", json={"path": rel})
    assert r.status_code == 201, r.text


def upload(api, path: str, data: bytes, *, conflict="keep_both", chunk=CHUNK, sha=None, key=None) -> dict:
    headers = {"Idempotency-Key": key} if key else {}
    r = api.post("/api/v1/transfers/uploads", headers=headers,
                 json={"path": path, "size": len(data), "sha256": sha or hashlib.sha256(data).hexdigest(), "conflict": conflict})
    assert r.status_code in (200, 201), r.text
    up = r.json()
    offset = up["offset"]
    while offset < len(data):
        r = api.put(f"/api/v1/transfers/uploads/{up['id']}/content", content=data[offset:offset + chunk],
                    headers={"Upload-Offset": str(offset), "Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text
        offset = int(r.headers["Upload-Offset"])
    r = api.post(f"/api/v1/transfers/uploads/{up['id']}/complete", json={"create_parents": True})
    return {"status": r.status_code, **r.json()}


def download(api, path: str, **headers) -> httpx.Response:
    return api.get("/api/v1/storage/files/content", params={"path": path}, headers=headers)


# --- bootstrap ------------------------------------------------------------------

def test_health_auth_and_readiness(api):
    assert httpx.get(f"{URL}/healthz").json() == {"status": "ok"}
    assert httpx.get(f"{URL}/api/v1/readiness").status_code == 401
    bad = httpx.get(f"{URL}/api/v1/readiness", headers={"Authorization": "Bearer hst_tok_x.nope"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "unauthenticated"
    modules = api.get("/api/v1/readiness").json()["modules"]
    for name in ("volume:backup-hdd", "storage:files", "storage:staging", "backups", "state_store"):
        assert modules[name]["state"] == "ready", (name, modules[name])
    assert api.get("/api/v1/bootstrap", headers={"Home-Services-Contract": "2"}).status_code == 400


def test_volume_is_verified_by_uuid(api):
    vols = api.get("/api/v1/storage/volumes").json()["volumes"]
    assert vols[0]["state"] == "verified" and vols[0]["filesystem"] == "exfat"
    cap = api.get("/api/v1/storage/capacity").json()
    assert cap["available_for_uploads_bytes"] <= cap["free_bytes"] - cap["volume_reserve_bytes"]


# --- files ----------------------------------------------------------------------

def test_upload_download_range_and_integrity(api, run_dir):
    data = os.urandom(9 * 1024 * 1024 + 123)  # three chunks, the last partial
    done = upload(api, f"{run_dir}/blob.bin", data)
    assert done["status"] == 200 and done["state"] == "published" and done["verification"]["sha256"] == "matched"
    r = download(api, done["published_path"])
    assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == hashlib.sha256(data).hexdigest()
    r = download(api, done["published_path"], Range="bytes=1048576-1048675")
    assert r.status_code == 206 and r.content == data[1048576:1048676]


def test_checksum_mismatch_publishes_nothing(api, run_dir):
    data = b"intended content\n"
    done = upload(api, f"{run_dir}/bad.txt", data, sha=hashlib.sha256(b"something else").hexdigest())
    assert done["status"] == 409 and done["error"]["code"] == "checksum_mismatch"
    names = [e["name"] for e in api.get("/api/v1/storage/files/entries", params={"path": run_dir}).json()["entries"]]
    assert "bad.txt" not in names


def test_client_restart_resumes_from_server_offset(api, run_dir):
    data = os.urandom(10 * 1024 * 1024)
    key = f"resume-{secrets.token_hex(4)}"
    r = api.post("/api/v1/transfers/uploads", headers={"Idempotency-Key": key},
                 json={"path": f"{run_dir}/resume.bin", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    up = r.json()
    api.put(f"/api/v1/transfers/uploads/{up['id']}/content", content=data[:CHUNK],
            headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"})
    # The client "crashes" and forgets everything except its idempotency key.
    again = api.post("/api/v1/transfers/uploads", headers={"Idempotency-Key": key},
                     json={"path": f"{run_dir}/resume.bin", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    assert again.status_code == 200 and again.json()["id"] == up["id"] and again.json()["offset"] == CHUNK
    stale = api.put(f"/api/v1/transfers/uploads/{up['id']}/content", content=data[:CHUNK],
                    headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"})
    assert stale.status_code == 409 and stale.json()["error"]["details"]["expected_offset"] == CHUNK
    done = upload(api, f"{run_dir}/resume.bin", data, key=key)
    assert done["state"] == "published"
    assert hashlib.sha256(download(api, done["published_path"]).content).hexdigest() == hashlib.sha256(data).hexdigest()


def test_connection_dropped_mid_chunk_does_not_advance_offset(api, run_dir):
    data = os.urandom(6 * 1024 * 1024)
    up = api.post("/api/v1/transfers/uploads", json={"path": f"{run_dir}/dropped.bin", "size": len(data),
                                                     "sha256": hashlib.sha256(data).hexdigest()}).json()
    u = urllib.parse.urlsplit(URL)
    ctx = ssl.create_default_context()
    with socket.create_connection((u.hostname, u.port or 443), timeout=30) as raw, ctx.wrap_socket(raw, server_hostname=u.hostname) as s:
        head = (f"PUT /api/v1/transfers/uploads/{up['id']}/content HTTP/1.1\r\nHost: {u.netloc}\r\n"
                f"Authorization: Bearer {TOKEN}\r\nUpload-Offset: 0\r\nContent-Type: application/octet-stream\r\n"
                f"Content-Length: {CHUNK}\r\n\r\n")
        s.sendall(head.encode() + data[:CHUNK // 3])  # then vanish mid-body
    time.sleep(2)
    status = api.get(f"/api/v1/transfers/uploads/{up['id']}").json()
    assert status["state"] == "open" and status["offset"] == 0
    r = api.put(f"/api/v1/transfers/uploads/{up['id']}/content", content=data[:CHUNK],
                headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"})
    assert r.status_code == 200 and int(r.headers["Upload-Offset"]) == CHUNK
    assert api.delete(f"/api/v1/transfers/uploads/{up['id']}").json()["state"] == "cancelled"


def test_concurrent_uploads_from_several_clients(run_dir):
    payloads = {f"{run_dir}/concurrent/{i}.bin": os.urandom(3 * 1024 * 1024 + i) for i in range(6)}
    same = os.urandom(512 * 1024)

    def one(path, data):
        with httpx.Client(base_url=URL, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120) as c:
            return upload(c, path, data, chunk=1024 * 1024)

    jobs = list(payloads.items()) + [(f"{run_dir}/concurrent/same.bin", same)] * 3
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda pair: one(*pair), jobs))
    assert all(r["state"] == "published" for r in results)
    with httpx.Client(base_url=URL, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120) as c:
        for path, data in payloads.items():
            assert hashlib.sha256(download(c, path).content).hexdigest() == hashlib.sha256(data).hexdigest()
        same_names = sorted(r["published_path"].rsplit("/", 1)[1] for r in results[len(payloads):])
        assert same_names == ["same (1).bin", "same (2).bin", "same.bin"]  # keep-both: three writers, nothing overwritten


def test_rename_move_conflict_and_case_only_rename_on_exfat(api, run_dir):
    upload(api, f"{run_dir}/m/a.txt", b"a\n")
    upload(api, f"{run_dir}/m/b.txt", b"b\n")
    api.post("/api/v1/storage/files/directories", json={"path": f"{run_dir}/m/sub"})
    r = api.post("/api/v1/storage/files/move", json={"from": f"{run_dir}/m/a.txt", "to": f"{run_dir}/m/sub/a2.txt"})
    assert r.status_code == 200 and r.json()["path"] == f"{run_dir}/m/sub/a2.txt"
    r = api.post("/api/v1/storage/files/move", json={"from": f"{run_dir}/m/sub/a2.txt", "to": f"{run_dir}/m/b.txt"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "destination_exists"
    r = api.post("/api/v1/storage/files/move", json={"from": f"{run_dir}/m/sub/a2.txt", "to": f"{run_dir}/m/b.txt", "conflict": "keep_both"})
    assert r.json()["path"] == f"{run_dir}/m/b (1).txt"
    r = api.post("/api/v1/storage/files/move", json={"from": f"{run_dir}/m/b.txt", "to": f"{run_dir}/m/B.txt"})
    assert r.status_code == 200, r.text  # exFAT is case-insensitive: this must rename, not report a conflict
    names = [e["name"] for e in api.get("/api/v1/storage/files/entries", params={"path": f"{run_dir}/m"}).json()["entries"]]
    assert "B.txt" in names and "b.txt" not in names
    r = api.post("/api/v1/storage/files/move", json={"from": f"{run_dir}/m", "to": f"{run_dir}/m/sub/inside"})
    assert r.status_code == 403


def test_trash_and_restore_with_keep_both(api, run_dir):
    upload(api, f"{run_dir}/t/doc.txt", b"original\n")
    item = api.post("/api/v1/storage/files/trash", json={"path": f"{run_dir}/t/doc.txt"}).json()
    assert item["original_path"] == f"{run_dir}/t/doc.txt"
    assert any(i["id"] == item["id"] for i in api.get("/api/v1/storage/files/trash").json()["items"])
    upload(api, f"{run_dir}/t/doc.txt", b"replacement\n")
    restored = api.post(f"/api/v1/storage/files/trash/{item['id']}/restore").json()
    assert restored == {"restored_path": f"{run_dir}/t/doc (1).txt", "renamed": True}
    assert download(api, f"{run_dir}/t/doc (1).txt").content == b"original\n"
    assert download(api, f"{run_dir}/t/doc.txt").content == b"replacement\n"


@pytest.mark.parametrize("path", ["../restic-repo/config", "../../restic-repo", "/mnt/workspace-backup/camera",
                                  "x/../../staging", "../staging/trash", "../../camera/events"])
def test_protected_areas_are_unreachable(api, path):
    for r in (api.get("/api/v1/storage/files/entries", params={"path": path}),
              api.get("/api/v1/storage/files/content", params={"path": path}),
              api.post("/api/v1/storage/files/trash", json={"path": path}),
              api.post("/api/v1/storage/files/move", json={"from": path, "to": "stolen"}),
              api.post("/api/v1/transfers/uploads", json={"path": path, "size": 1})):
        assert r.status_code == 403 and r.json()["error"]["code"] == "path_not_allowed", (path, r.status_code, r.text)


# --- backups --------------------------------------------------------------------

def _small_file_in(api, snap: dict, max_depth: int = 3) -> dict | None:
    queue = [(snap["paths"][0], 0)]
    while queue:
        path, depth = queue.pop(0)
        entries = api.get(f"/api/v1/backups/snapshots/{snap['id']}/entries", params={"path": path}).json()["entries"]
        files = [e for e in entries if e["kind"] == "file" and 0 < (e["size"] or 0) < 1_000_000]
        if files:
            return files[0]
        if depth < max_depth:
            queue += [(e["path"], depth + 1) for e in entries if e["kind"] == "directory"][:5]
    return None


def test_snapshot_browse_download_and_isolated_restore_match(api):
    snaps = api.get("/api/v1/backups/snapshots").json()["snapshots"]
    # The Pi's own series currently snapshots an empty folder, so use the newest series that holds files.
    snap = next(s for s in snaps if (s.get("contents") or {}).get("files", 1))
    file_entry = _small_file_in(api, snap)
    assert file_entry is not None, snap
    archived = api.get(f"/api/v1/backups/snapshots/{snap['id']}/content", params={"path": file_entry["path"]})
    assert archived.status_code == 200 and len(archived.content) == file_entry["size"]
    key = f"live-restore-{secrets.token_hex(4)}"
    job = api.post("/api/v1/backups/restores", headers={"Idempotency-Key": key},
                   json={"snapshot_id": snap["id"], "include": [file_entry["path"]]}).json()
    for _ in range(120):
        job = api.get(f"/api/v1/jobs/{job['id']}").json()
        if job["state"] in ("succeeded", "failed", "interrupted", "cancelled"):
            break
        time.sleep(1)
    assert job["state"] == "succeeded", job
    restored_path = f"{job['result']['destination']['path']}{file_entry['path']}"
    restored = download(api, restored_path)
    assert hashlib.sha256(restored.content).hexdigest() == hashlib.sha256(archived.content).hexdigest()
    local = file_entry["path"]  # the Mac series: compare with the live original when it has not changed since
    if os.path.isfile(local) and os.path.getsize(local) == file_entry["size"]:
        with open(local, "rb") as f:
            original = hashlib.sha256(f.read()).hexdigest()
        if file_entry["modified_at"] and abs(os.path.getmtime(local) - _ts(file_entry["modified_at"])) < 2:
            assert original == hashlib.sha256(restored.content).hexdigest()
    api.post("/api/v1/storage/files/trash", json={"path": job["result"]["destination"]["path"]})


def _ts(value: str) -> float:
    from home_services.backups.restic import snapshot_timestamp
    return snapshot_timestamp(value)


def test_protection_reflects_repository_snapshots(api):
    groups = api.get("/api/v1/backups/protection").json()["groups"]
    pi = next(g for g in groups if g["hostname"] == "pi.example.local")
    assert pi["protection"]["evidence"] == "repository_snapshot"
    assert pi["protection"]["state"] in ("current", "overdue")
    if pi["protection"]["contents"]:
        assert pi["protection"]["empty"] == (pi["protection"]["contents"]["files"] == 0)


# --- tokens and legacy contract -----------------------------------------------------

@needs_ssh
def test_scoped_token_boundaries_revocation_and_expiry():
    token, token_id = make_token("camera-monitor")
    h = {"Authorization": f"Bearer {token}"}
    assert httpx.get(f"{URL}/api/v1/cameras", headers=h).status_code == 200
    r = httpx.get(f"{URL}/api/v1/storage/files/entries", headers=h)
    assert r.status_code == 403 and r.json()["error"]["details"]["required_scope"] == "storage:read"
    ssh(f"{CLI} tokens revoke {token_id}")
    r = httpx.get(f"{URL}/api/v1/cameras", headers=h)
    assert r.status_code == 401 and r.json()["error"]["message"] == "token revoked"
    short, short_id = make_token("files-backups", ttl_days=0.0002)  # about 17 seconds
    hs = {"Authorization": f"Bearer {short}"}
    assert httpx.get(f"{URL}/api/v1/storage/capacity", headers=hs).status_code == 200
    time.sleep(20)
    r = httpx.get(f"{URL}/api/v1/storage/capacity", headers=hs)
    assert r.status_code == 401 and r.json()["error"]["message"] == "token expired"
    ssh(f"{CLI} tokens revoke {short_id}")


@needs_ssh
def test_legacy_routes_match_the_predecessor():
    token, token_id = make_token("home-platform-legacy")
    try:
        ours = httpx.get(f"{URL}/api/storage/contract", headers={"X-Workspace-Token": token}).json()
        dest = httpx.get(f"{URL}/api/backup/destination", headers={"X-Workspace-Token": token}).json()
        status = httpx.get(f"{URL}/api/backup/status", headers={"X-Workspace-Token": token}).json()
        theirs = json.loads(ssh(". /etc/workspace-util/secrets.env 2>/dev/null; curl -fsS -H \"X-Workspace-Token: $WORKSPACE_DASHBOARD_TOKEN\" "
                                "http://$(tailscale ip -4):8787/api/storage/contract").stdout)
        their_dest = json.loads(ssh(". /etc/workspace-util/secrets.env 2>/dev/null; curl -fsS -H \"X-Workspace-Token: $WORKSPACE_DASHBOARD_TOKEN\" "
                                    "http://$(tailscale ip -4):8787/api/backup/destination").stdout)
    finally:
        ssh(f"{CLI} tokens revoke {token_id}")
    assert set(ours) == set(theirs)
    for field in ("production_sync_safe", "workspace_disk_mounted", "project_directory_creation_allowed"):
        assert ours[field] == theirs[field], field
    assert (theirs["mode"], ours["mode"]) in (("test", "pending"), ("production", "production"))
    assert set(their_dest) <= set(dest)
    for field in ("state", "uuid", "mount", "filesystem", "device"):
        assert dest[field] == their_dest[field], field
    assert status["state"] in ("completed", "running", "failed", "unknown")
    assert httpx.get(f"{URL}/api/storage/contract", headers={"X-Workspace-Token": token}).status_code == 401


# --- cameras (non-destructive: no arming, no deletion on the real camera) -----------

CAM = os.environ.get("HOME_SERVICES_LIVE_CAMERA", "workspace-pi-camera")
SLOW = os.environ.get("HOME_SERVICES_LIVE_SLOW") == "1"


def _ffprobe_duration(data: bytes, suffix: str) -> float:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(data)
        f.flush()
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", f.name],
                             capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def test_camera_status_snapshot_and_active_hour_playback(api):
    cam = api.get(f"/api/v1/cameras/{CAM}").json()
    assert cam["status"]["state"] == "recording", cam["status"]
    assert all(c["state"] == "available" for n, c in cam["capabilities"].items() if n != "notifications")
    snap = api.get(f"/api/v1/cameras/{CAM}/snapshot", params={"width": 320})
    assert snap.status_code == 200 and snap.content[:2] == b"\xff\xd8"
    hours = api.get(f"/api/v1/cameras/{CAM}/recordings", params={"kind": "continuous", "limit": 1}).json()["recordings"]
    assert hours and hours[0]["active"] is True
    pb = api.post(f"/api/v1/cameras/{CAM}/recordings/{hours[0]['id']}/playback-sessions").json()
    playlist = httpx.get(URL + pb["url"]).text
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in playlist and "#EXT-X-ENDLIST" not in playlist
    first = next(x for x in playlist.splitlines() if x.endswith(".ts"))
    seg = httpx.get(URL + pb["url"].rsplit("/", 1)[0] + "/" + first)
    assert seg.status_code == 200 and _ffprobe_duration(seg.content, ".ts") > 1


def test_motion_recording_export_and_reversible_protection(api):
    motions = api.get(f"/api/v1/cameras/{CAM}/recordings", params={"kind": "motion", "limit": 20}).json()["recordings"]
    done = [r for r in motions if not r["active"] and r["playable"]]
    if not done:
        pytest.skip("no finished motion recording on the camera yet")
    rec = done[0]
    exp = api.post(f"/api/v1/cameras/{CAM}/recordings/{rec['id']}/exports")
    assert exp.status_code == 201, exp.text
    body = httpx.get(URL + exp.json()["download_url"], timeout=120).content
    assert len(body) == exp.json()["size_bytes"] and _ffprobe_duration(body, ".mp4") > 5
    original = rec["protected"]
    r = api.put(f"/api/v1/cameras/{CAM}/recordings/{rec['id']}/protected", json={"protected": not original})
    assert r.status_code == 200 and r.json()["protected"] is (not original)
    if not original:
        d = api.delete(f"/api/v1/cameras/{CAM}/recordings/{rec['id']}")
        assert d.status_code == 409 and d.json()["error"]["code"] == "recording_protected"  # nothing deleted
    r = api.put(f"/api/v1/cameras/{CAM}/recordings/{rec['id']}/protected", json={"protected": original})
    assert r.json()["protected"] is original


@pytest.mark.skipif(not SLOW, reason="set HOME_SERVICES_LIVE_SLOW=1 (about 6 minutes)")
def test_live_session_slides_while_fetched_and_expires_when_idle(api):
    live = api.post(f"/api/v1/cameras/{CAM}/live-sessions").json()
    assert live["expiry"] == "sliding"
    for _ in range(12):  # 4 minutes, well past the 2-minute idle window
        assert httpx.get(URL + live["url"]).status_code == 200
        time.sleep(20)
    time.sleep(130)
    r = httpx.get(URL + live["url"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "media_session_expired"
