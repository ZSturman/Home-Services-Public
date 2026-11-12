"""Contract fixtures.

`run_scenario` drives the real gateway (in-process) against an isolated
fixture volume with a fixed clock and sequential IDs, recording every
request/response pair. `export_contract` writes those pairs and the OpenAPI
document under `contracts/v1/`. Contract tests replay the scenario and check
the committed examples: OpenAPI must match exactly; each example must match
in status code and JSON shape (values such as restic snapshot IDs, capacity
bytes, and file mtimes legitimately vary between runs).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

FIXED_START = 1792483200.0  # 2026-10-20T08:00:00Z
FIXED_MTIME = 1792400000.0
PLACEHOLDER_HOST = "https://host.example.net:8443"


def _fixture_config(root: Path) -> Path:
    cfg = root / "config.toml"
    cfg.write_text(f"""
environment = "development"
data_dir = "data"
[network]
bind_addresses = ["127.0.0.1"]
[mounts]
probe = "fixture"
fixture_table = "mounts.json"
[[volumes]]
id = "backup-hdd"
mount = "volumes/backup-hdd"
uuid = "DEV-BACKUP-0001"
fstype = "exfat"
[volumes.roots]
files = "home-services/files"
staging = "home-services/staging"
recordings = "home-services/recordings"
restic_repository = "restic-repo"
legacy_media = "camera"
[limits]
volume_reserve_bytes = 1000000
staging_cap_bytes = 2000000000
[backup]
enabled = true
password_file = "secrets/restic-password"
companion_repository = "sftp:backup@host.example.net:/mnt/workspace-backup/restic-repo"
[[cameras.devices]]
id = "workspace-pi-camera"
name = "Workspace Pi Camera"
[legacy_contract]
backup_volume = "backup-hdd"
""")
    return cfg


def _touch_tree(root: Path) -> None:
    for p in [root, *root.rglob("*")]:
        os.utime(p, (FIXED_MTIME, FIXED_MTIME), follow_symlinks=False)


def run_scenario(work: Path, *, with_restic: bool = True) -> tuple[list[dict], dict]:
    from fastapi.testclient import TestClient

    from .api.app import create_app
    from .config import load_config
    from .devfixtures import create_dev_fixtures
    from .runtime import FixedClock, SequentialIds
    from .services import build_services
    from .worker import Stop, work_once

    fx = create_dev_fixtures(work, with_restic=with_restic)
    cfg = load_config(_fixture_config(work))
    clock, ids = FixedClock(FIXED_START), SequentialIds()
    s = build_services(cfg, clock=clock, ids=ids)
    app = create_app(s)
    client = TestClient(app, base_url=PLACEHOLDER_HOST)
    files_token, _ = s.tokens.create("Files & Backups (iPhone)", ["storage:read", "storage:write", "transfers:write", "jobs:read",
                                                                   "jobs:write", "backups:read", "backups:restore"])
    camera_token, _ = s.tokens.create("Camera Monitor (iPhone)", ["cameras:read", "cameras:control", "media:read", "jobs:read"])
    companion_token, _ = s.tokens.create("Files & Backups Mac companion", ["backups:report", "backups:read", "jobs:read"])
    legacy_token, _ = s.tokens.create("Home Platform", ["legacy:storage-contract"])
    examples: list[dict] = []

    def call(name, method, path, *, token=None, json_body=None, content=None, headers=None, params=None, body_kind="json", note=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        h.setdefault("Home-Services-Contract", "1")
        r = client.request(method, path, json=json_body, content=content, headers=h, params=params)
        if body_kind == "json":
            body = r.json() if r.content else None
        else:
            body = {"bytes": len(r.content), "sha256": hashlib.sha256(r.content).hexdigest()}
        resp_headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "upload-offset", "home-services-contract",
                                                                              "content-disposition", "accept-ranges", "content-range", "cache-control")}
        req = {"method": method, "path": path, "headers": h}
        if params:
            req["query"] = params
        if json_body is not None:
            req["body"] = json_body
        if content is not None:
            req["body_bytes"] = len(content)
        examples.append({"name": name, **({"note": note} if note else {}), "request": req,
                         "response": {"status": r.status_code, "headers": resp_headers, "body": body, "body_kind": body_kind}})
        return r

    files_root = Path(fx["mount"]) / "home-services/files"
    (files_root / "Documents").mkdir(exist_ok=True)
    (files_root / "Documents" / "existing.txt").write_text("already here\n")
    _touch_tree(files_root)

    call("health_public", "GET", "/healthz")
    call("bootstrap", "GET", "/api/v1/bootstrap", token=files_token)
    call("capabilities", "GET", "/api/v1/capabilities", token=files_token)
    call("readiness", "GET", "/api/v1/readiness", token=files_token)
    call("error_contract_version_unsupported", "GET", "/api/v1/bootstrap", token=files_token, headers={"Home-Services-Contract": "2"})
    call("error_unauthenticated", "GET", "/api/v1/capabilities")
    call("error_forbidden_scope", "GET", "/api/v1/storage/files/entries", token=camera_token)

    call("storage_volumes", "GET", "/api/v1/storage/volumes", token=files_token)
    call("storage_capacity", "GET", "/api/v1/storage/capacity", token=files_token)
    call("storage_mkdir", "POST", "/api/v1/storage/files/directories", token=files_token, json_body={"path": "Photos"})
    _touch_tree(files_root)
    call("storage_list", "GET", "/api/v1/storage/files/entries", token=files_token, params={"path": ""})
    call("storage_entry", "GET", "/api/v1/storage/files/entry", token=files_token, params={"path": "Documents/existing.txt"})
    call("storage_entry_missing", "GET", "/api/v1/storage/files/entry", token=files_token, params={"path": "Documents/nope.txt"})

    data = b"hello from the phone\n" * 3
    digest = hashlib.sha256(data).hexdigest()
    r = call("upload_create", "POST", "/api/v1/transfers/uploads", token=files_token, headers={"Idempotency-Key": "ios-transfer-7F3A"},
             json_body={"root": "files", "path": "Documents/existing.txt", "size": len(data), "sha256": digest, "conflict": "keep_both"})
    upload_id = r.json()["id"]
    call("upload_create_replay", "POST", "/api/v1/transfers/uploads", token=files_token, headers={"Idempotency-Key": "ios-transfer-7F3A"},
         json_body={"root": "files", "path": "Documents/existing.txt", "size": len(data), "sha256": digest, "conflict": "keep_both"},
         note="same Idempotency-Key and request: the original session is returned with 200")
    call("upload_append_first", "PUT", f"/api/v1/transfers/uploads/{upload_id}/content", token=files_token,
         headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"}, content=data[:20])
    call("upload_append_wrong_offset", "PUT", f"/api/v1/transfers/uploads/{upload_id}/content", token=files_token,
         headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"}, content=data[:20],
         note="resume from details.expected_offset")
    call("upload_status", "GET", f"/api/v1/transfers/uploads/{upload_id}", token=files_token)
    call("upload_append_rest", "PUT", f"/api/v1/transfers/uploads/{upload_id}/content", token=files_token,
         headers={"Upload-Offset": "20", "Content-Type": "application/octet-stream"}, content=data[20:])
    call("upload_complete", "POST", f"/api/v1/transfers/uploads/{upload_id}/complete", token=files_token, json_body={"create_parents": False},
         note="destination existed, so keep_both published 'existing (1).txt'")
    call("storage_download", "GET", "/api/v1/storage/files/content", token=files_token, params={"path": "Documents/existing (1).txt"},
         body_kind="bytes")
    call("storage_download_range", "GET", "/api/v1/storage/files/content", token=files_token, params={"path": "Documents/existing (1).txt"},
         headers={"Range": "bytes=0-4"}, body_kind="bytes")
    call("storage_move", "POST", "/api/v1/storage/files/move", token=files_token,
         json_body={"from": "Documents/existing (1).txt", "to": "Photos/from-phone.txt"})
    call("storage_move_conflict", "POST", "/api/v1/storage/files/move", token=files_token,
         json_body={"from": "Photos/from-phone.txt", "to": "Documents/existing.txt", "conflict": "fail"},
         note="conflict defaults to fail; keep_both picks 'name (1).ext'")
    call("storage_trash", "POST", "/api/v1/storage/files/trash", token=files_token, json_body={"path": "Documents/existing.txt"})
    r = call("storage_trash_list", "GET", "/api/v1/storage/files/trash", token=files_token)
    trash_id = r.json()["items"][0]["id"]
    call("storage_trash_restore", "POST", f"/api/v1/storage/files/trash/{trash_id}/restore", token=files_token)

    r = call("job_create_cleanup", "POST", "/api/v1/jobs", token=files_token, headers={"Idempotency-Key": "cleanup-2026-10-20"},
             json_body={"kind": "storage.cleanup", "params": {}})
    job_id = r.json()["id"]
    call("job_get_waiting", "GET", f"/api/v1/jobs/{job_id}", token=files_token)
    work_once(s, "storage", "fixture-worker:storage", Stop())
    call("job_get_succeeded", "GET", f"/api/v1/jobs/{job_id}", token=files_token,
         note="ran in the separately supervised storage worker")
    r = call("job_create_cleanup_second", "POST", "/api/v1/jobs", token=files_token, headers={"Idempotency-Key": "cleanup-2026-10-20b"},
             json_body={"kind": "storage.cleanup", "params": {}})
    call("job_cancel", "POST", f"/api/v1/jobs/{r.json()['id']}/cancel", token=files_token)
    call("job_list", "GET", "/api/v1/jobs", token=files_token, params={"limit": 5})

    call("backups_repository", "GET", "/api/v1/backups/repository", token=files_token)
    snaps, src_root = [], None
    if with_restic:
        r = call("backups_snapshots", "GET", "/api/v1/backups/snapshots", token=files_token)
        snaps = r.json().get("snapshots", [])
        if snaps:
            snap = snaps[0]["id"]
            src_root = snaps[0]["paths"][0]
            call("backups_snapshot_entries", "GET", f"/api/v1/backups/snapshots/{snap}/entries", token=files_token,
                 params={"path": f"{src_root}/Projects/Demo"})
            call("backups_snapshot_file", "GET", f"/api/v1/backups/snapshots/{snap}/content", token=files_token,
                 params={"path": f"{src_root}/Projects/Demo/notes.txt"}, body_kind="bytes")
            r = call("backups_restore_create", "POST", "/api/v1/backups/restores", token=files_token,
                     headers={"Idempotency-Key": "restore-demo-1"}, json_body={"snapshot_id": snap, "include": [f"{src_root}/Projects/Demo"]})
            restore_job = r.json()["id"]
            work_once(s, "backup", "fixture-worker:backup", Stop())
            call("backups_restore_job_succeeded", "GET", f"/api/v1/jobs/{restore_job}", token=files_token,
                 note="restored into a fresh files/Restores/<job id> directory")

    source_path = src_root if with_restic and snaps else "/Users/example/Workspace"
    r = call("backup_source_register", "POST", "/api/v1/backups/sources", token=companion_token,
             json_body={"host": "fixture-mac", "path": source_path, "tag": "mac-workspace"})
    source_id = r.json()["id"]
    r2 = s.backups.register_source(host="Example-MacBook-Pro.local", path="/Users/example/Archive", tag="mac-archive",
                                   scheduler="files-backups-companion", principal_id=None)[0]
    call("backup_source_register_conflict", "POST", "/api/v1/backups/sources", token=companion_token,
         json_body={"host": "fixture-mac", "path": source_path, "tag": "mac-workspace", "scheduler": "legacy-launchagent"},
         note="one scheduler owns a source")
    r = call("backup_run_start", "POST", f"/api/v1/backups/sources/{source_id}/runs", token=companion_token,
             headers={"Idempotency-Key": "mac-2026-10-20T02:00"}, json_body={"scheduler": "files-backups-companion"})
    run_id = r.json()["id"]
    call("backup_run_start_repository_busy", "POST", f"/api/v1/backups/sources/{r2['id']}/runs", token=companion_token,
         headers={"Idempotency-Key": "archive-2026-10-20T02:00"}, json_body={"scheduler": "files-backups-companion"})
    clock.advance(30)
    call("backup_run_heartbeat", "POST", f"/api/v1/backups/runs/{run_id}/heartbeat", token=companion_token,
         json_body={"progress": {"percent_done": 0.42, "files_done": 1200, "bytes_done": 52428800, "total_files": 3000, "total_bytes": 125829120}})
    clock.advance(60)
    call("backup_run_finish_unknown_snapshot", "POST", f"/api/v1/backups/runs/{run_id}/finish", token=companion_token,
         json_body={"state": "succeeded", "snapshot_id": "4515c74846f53f1e9cc02631c6e74d0594c105c60ecbb5876f35eeaba39a8fc6"},
         note="success is accepted only for a snapshot the repository contains for this source; the run stays live")
    if with_restic and snaps:
        call("backup_run_finish", "POST", f"/api/v1/backups/runs/{run_id}/finish", token=companion_token,
             json_body={"state": "succeeded", "snapshot_id": snaps[0]["short_id"],
                        "summary": {"files_new": 12, "files_changed": 3, "data_added": 1048576}},
             note="the stored snapshot_id is the repository's full ID")
        call("backup_run_late_report_rejected", "POST", f"/api/v1/backups/runs/{run_id}/heartbeat", token=companion_token, json_body={})
        call("backup_source_status", "GET", f"/api/v1/backups/sources/{source_id}", token=files_token,
             note="protection comes from the newest repository snapshot with the source's tag and path")
        call("backups_protection", "GET", "/api/v1/backups/protection", token=files_token)
    r = call("backup_source_register_external", "POST", "/api/v1/backups/sources", token=companion_token,
             json_body={"host": "Example-MacBook-Pro.local", "path": "/Users/example/Workspace/Projects", "tag": "job-mac-projects",
                        "repository": "external:files-backups/mac-projects"},
             note="status-only: the companion's own repository, which Home Services cannot read")
    ext_id = r.json()["id"]
    r = call("backup_run_start_external", "POST", f"/api/v1/backups/sources/{ext_id}/runs", token=companion_token,
             headers={"Idempotency-Key": "mac-projects-2026-10-21T02:00"}, json_body={"scheduler": "files-backups-companion"},
             note="external repositories have their own writer lease")
    call("backup_run_finish_external", "POST", f"/api/v1/backups/runs/{r.json()['id']}/finish", token=companion_token,
         json_body={"state": "succeeded", "snapshot_id": "af8c64bee44c88267d1b97c87092085d8dc313c80e24a0f9df1e9ee0f1cf9053",
                    "summary": {"total_files": 684631, "total_bytes": 114681330501, "data_added": 52428800}})
    call("backup_source_status_external", "GET", f"/api/v1/backups/sources/{ext_id}", token=files_token,
         note="protection.evidence is companion_report and verified is false")

    call("cameras_list", "GET", "/api/v1/cameras", token=camera_token)
    call("camera_arm_unavailable", "PUT", "/api/v1/cameras/workspace-pi-camera/armed", token=camera_token, json_body={"armed": True})
    call("camera_live_session_unavailable", "POST", "/api/v1/cameras/workspace-pi-camera/live-sessions", token=camera_token)
    r = call("media_legacy_events", "GET", "/api/v1/media/legacy/events", token=camera_token)
    ev = r.json()["events"][0]["id"]
    r = call("media_session_create", "POST", "/api/v1/media/sessions", token=camera_token,
             json_body={"kind": "legacy_event_media", "event_id": ev, "name": "recording.mp4"})
    url = r.json()["url"]
    call("media_session_fetch", "GET", url, body_kind="bytes", note="no Authorization header: the session URL is the credential")
    clock.advance(cfg.media_session_seconds + 1)
    call("media_session_expired", "GET", url)

    call("legacy_storage_contract", "GET", "/api/storage/contract", headers={"X-Workspace-Token": legacy_token, "Home-Services-Contract": "1"})
    call("legacy_backup_destination", "GET", "/api/backup/destination", headers={"X-Workspace-Token": legacy_token})
    if with_restic:
        call("legacy_backup_status", "GET", "/api/backup/status", headers={"X-Workspace-Token": legacy_token},
             note="derived from repository snapshots; Home Platform reads state, warning, and error")

    # --- cameras with the native hls-recorder engine (deterministic fixture rows, no ffmpeg) ---
    cam_now = clock.now()
    cam_cfg = work / "camera-config.toml"
    text = _fixture_config(work).read_text()
    cam_cfg.write_text(text.replace('data_dir = "data"', 'data_dir = "camera-data"').replace(
        '[[cameras.devices]]\nid = "workspace-pi-camera"\nname = "Workspace Pi Camera"\n',
        '[notifications]\nenabled = true\nntfy_url_file = "secrets/ntfy-url"\n[cameras]\nengine = "hls-recorder"\n'
        '[[cameras.devices]]\nid = "workspace-pi-camera"\nname = "Workspace Pi Camera"\n'
        f'source = "{work}/live/index.m3u8"\n'))
    (work / "live").mkdir(exist_ok=True)
    (work / "live" / "segment100.ts").write_bytes(b"\x47" + b"\x00" * 187)
    (work / "live" / "index.m3u8").write_text("#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:100\n"
                                              "#EXTINF:2.000000,\nsegment100.ts\n")
    for f in (work / "live").iterdir():
        os.utime(f, (cam_now - 2, cam_now - 2))
    cs = build_services(load_config(cam_cfg), clock=clock, ids=ids)
    rec_root = Path(fx["mount"]) / "home-services/recordings"
    t_ev = cam_now - 900
    for i in range(3):
        rel = f"workspace-pi-camera/2026-10-20/{i:02d}-fixture.ts"
        (rec_root / rel).parent.mkdir(parents=True, exist_ok=True)
        (rec_root / rel).write_bytes(b"\x47" + b"\x00" * 187 * (i + 1))
        cs.db.execute("INSERT INTO camera_chunks (camera_id, start_ts, duration, path, bytes, motion_frames) VALUES (?,?,?,?,?,?)",
                      ("workspace-pi-camera", t_ev - 30 + 60 * i, 60.0, rel, 188 * (i + 1), 4 if i == 1 else 0))
    cs.db.execute("INSERT INTO camera_events (id, camera_id, started_at, last_motion_at, ended_at, end_reason, notified)"
                  " VALUES ('evt_workspace-pi-camera_fixture_1', 'workspace-pi-camera', ?, ?, ?, 'quiet', 1)", (t_ev, t_ev + 22, t_ev + 42))
    cs.db.execute("INSERT INTO camera_events (id, camera_id, started_at, last_motion_at) VALUES"
                  " ('evt_workspace-pi-camera_fixture_2', 'workspace-pi-camera', ?, ?)", (cam_now - 8, cam_now - 3))
    cs.db.execute("INSERT INTO camera_status (camera_id, recorder_heartbeat_at, last_segment_at, state, detail, updated_at)"
                  " VALUES ('workspace-pi-camera', ?, ?, 'recording', '', ?)", (cam_now - 1, cam_now - 2, cam_now - 1))
    first_client, client = client, TestClient(create_app(cs), base_url=PLACEHOLDER_HOST)
    cam_token, _ = cs.tokens.create("Camera Monitor (iPhone)", ["cameras:read", "cameras:control", "media:read", "jobs:read"])
    base = "/api/v1/cameras/workspace-pi-camera"
    call("camera_recording_get", "GET", base, token=cam_token, note="engine hls-recorder: status and capabilities from the live recorder")
    call("camera_arm", "PUT", f"{base}/armed", token=cam_token, json_body={"armed": True},
         note="arming enables motion notifications; recording runs either way")
    r = call("camera_live_session", "POST", f"{base}/live-sessions", token=cam_token)
    call("camera_live_playlist", "GET", r.json()["url"], body_kind="bytes", note="no Authorization header; sliding expiry while fetched")
    call("camera_recordings", "GET", f"{base}/recordings", token=cam_token, params={"limit": 10})
    done_id, active_id = "evt_workspace-pi-camera_fixture_1", "evt_workspace-pi-camera_fixture_2"
    r = call("camera_playback_session", "POST", f"{base}/recordings/{done_id}/playback-sessions", token=cam_token)
    call("camera_playback_playlist", "GET", r.json()["url"], body_kind="bytes", note="VOD HLS over the stored chunks")
    call("camera_delete_active_rejected", "DELETE", f"{base}/recordings/{active_id}", token=cam_token)
    call("camera_protect", "PUT", f"{base}/recordings/{done_id}/protected", token=cam_token, json_body={"protected": True})
    call("camera_delete_protected_rejected", "DELETE", f"{base}/recordings/{done_id}", token=cam_token)
    cs.camera_store.set_protected("workspace-pi-camera", done_id, False)
    call("camera_delete", "DELETE", f"{base}/recordings/{done_id}", token=cam_token,
         note="removes the recording's chunks except any overlapping a protected or active recording")
    client = first_client
    cs.db.close()

    table = Path(fx["mount_table"])
    original = table.read_text()
    tbl = json.loads(original)
    tbl.pop(fx["mount"])
    table.write_text(json.dumps(tbl))
    call("error_volume_missing", "GET", "/api/v1/storage/files/entries", token=files_token, note="disk unplugged: fails closed")
    call("error_volume_missing_upload", "POST", "/api/v1/transfers/uploads", token=files_token,
         json_body={"root": "files", "path": "Documents/new.txt", "size": 3})
    tbl[fx["mount"]] = {"uuid": "SOME-OTHER-DISK", "fstype": "exfat", "device": "/dev/sdb1"}
    table.write_text(json.dumps(tbl))
    call("error_wrong_volume", "GET", "/api/v1/storage/files/entries", token=files_token, note="a different disk at the same mount point")
    table.write_text(original)

    # Article relay: independent from the disk/camera modules and never a publisher.
    article_mac, _ = s.tokens.create("Article Mac", ["articles:read", "articles:sync"])
    article_phone, _ = s.tokens.create("Article iPhone", ["articles:read", "articles:review"])
    endpoint = "/api/v1/articles/review"
    workspace = {"workspace_id": "fixture-articles"}
    article = {"id": "fixture-article", "title": "A reviewable article", "markdown": "An isolated article body.",
               "revision": "revision-1", "review_digest": "digest-1", "workspace": {"complete": True}}
    call("articles_register", "POST", endpoint, token=article_mac, json_body={"action": "register", **workspace})
    call("articles_snapshot", "POST", endpoint, token=article_mac, json_body={"action": "sync_snapshot", "article": article, **workspace})
    call("articles_list", "POST", endpoint, token=article_phone, json_body={"action": "list"})
    call("articles_get", "POST", endpoint, token=article_phone, json_body={"action": "get", "article_id": article["id"]})
    edit = {"action": "save_article", "article_id": article["id"], "revision": "revision-1", "request_id": "fixture-edit", "markdown": "Edited on the phone.", **workspace}
    call("articles_review_queue", "POST", endpoint, token=article_phone, json_body=edit)
    call("articles_review_replay", "POST", endpoint, token=article_phone, json_body=edit)
    call("articles_pending", "POST", endpoint, token=article_mac, json_body={"action": "pending", **workspace})
    result = {"status": "ok", "article": {**article, "revision": "revision-2", "markdown": "Edited on the phone."}}
    call("articles_applied", "POST", endpoint, token=article_mac,
         json_body={"action": "resolve", "request_id": "fixture-edit", "state": "applied", "result": result, **workspace})
    call("articles_stale_review", "POST", endpoint, token=article_phone, json_body={**edit, "request_id": "fixture-stale"})
    call("articles_publish_rejected", "POST", endpoint, token=article_phone, json_body={"action": "publish", **workspace})

    # Opportunity relay (1.4): the Mac applies queued phone requests; the relay never sends email.
    opp_mac, _ = s.tokens.create("Opportunity Mac", ["opportunities:read", "opportunities:sync"])
    opp_phone, _ = s.tokens.create("Opportunity iPhone", ["opportunities:read", "opportunities:review"])
    endpoint = "/api/v1/opportunities/relay"
    workspace = {"workspace_id": "fixture-opportunities"}
    case_key = "/api/v2/review/fixture-case"
    case = {"id": "fixture-case", "revision": 4, "stage": "Awaiting Review", "decision": ""}
    call("opportunities_register", "POST", endpoint, token=opp_mac, json_body={"action": "register", **workspace})
    call("opportunities_sync", "POST", endpoint, token=opp_mac,
         json_body={"action": "sync", "resources": {case_key: case, "/api/mobile/summary": {"to_review": 1}}, **workspace})
    call("opportunities_get", "POST", endpoint, token=opp_phone, json_body={"action": "get", "keys": [case_key, "/api/mobile/summary"]})
    decision = {"action": "submit", "request_id": "fixture-decision", "path": case_key + "/decision",
                "body": {"revision": 4, "decision": "approve"}, **workspace}
    call("opportunities_submit", "POST", endpoint, token=opp_phone, json_body=decision)
    call("opportunities_submit_chained", "POST", endpoint, token=opp_phone,
         json_body={**decision, "request_id": "fixture-notes", "path": case_key + "/notes", "after": "fixture-decision",
                    "body": {"revision": 4, "notes": "Queued after the decision."}})
    call("opportunities_pending", "POST", endpoint, token=opp_mac, json_body={"action": "pending", **workspace})
    applied = {**case, "revision": 5, "stage": "Approved", "decision": "approve"}
    call("opportunities_applied", "POST", endpoint, token=opp_mac,
         json_body={"action": "resolve", "request_id": "fixture-decision", "state": "applied",
                    "result": {"status": "ok", "case_revision": 5, "response": applied}, "resources": {case_key: applied}, **workspace})
    call("opportunities_operations", "POST", endpoint, token=opp_phone,
         json_body={"action": "operations", "request_ids": ["fixture-decision", "fixture-notes"]})
    call("opportunities_stale_submit", "POST", endpoint, token=opp_phone, json_body={**decision, "request_id": "fixture-stale"})
    call("opportunities_dispatch_rejected", "POST", endpoint, token=opp_phone,
         json_body={**decision, "request_id": "fixture-dispatch", "path": "/api/v2/outbox/dispatch", "body": {}})

    # Contract 1.6: QR/paste invitation, exchange retry and scoped connection settings.
    from .connections import Connections
    connection_admin = Connections(s)
    invitation = connection_admin.invite(preset="home-page", name="Home Page fixture", url=PLACEHOLDER_HOST)
    proof = {k: invitation["invitation"][k] for k in ("invitation_id", "secret")}
    call("pairing_preview", "POST", "/api/v1/pairing/preview", json_body=proof)
    claim = {**proof, "client_secret": "fixture-client-secret-" + "x" * 32, "device_name": "Fixture phone"}
    paired = call("pairing_exchange", "POST", "/api/v1/pairing/exchange", json_body=claim).json()
    call("pairing_exchange_replay", "POST", "/api/v1/pairing/exchange", json_body=claim)
    call("pairing_competing_claim", "POST", "/api/v1/pairing/exchange", json_body={**claim, "client_secret": "y" * 43})
    call("connection_read", "GET", "/api/v1/connection", token=paired["token"])
    call("connection_cannot_escalate", "PATCH", "/api/v1/connection", token=paired["token"], json_body={"scopes": ["storage:write"]})
    call("connection_toggle_off", "PATCH", "/api/v1/connection", token=paired["token"], json_body={"scopes": []})
    call("connection_disconnect", "DELETE", "/api/v1/connection", token=paired["token"])
    call("connection_revoked", "GET", "/api/v1/connection", token=paired["token"])
    expires = connection_admin.invite(preset="home-page", name="Expired fixture", url=PLACEHOLDER_HOST)
    clock.advance(300)
    call("pairing_expired", "POST", "/api/v1/pairing/preview",
         json_body={k: expires["invitation"][k] for k in ("invitation_id", "secret")})

    openapi = app.openapi()
    s.db.close()
    return examples, openapi


def _scrub(obj, work: Path):
    """Replace the temporary fixture directory with a stable placeholder."""
    text = json.dumps(obj, indent=2, sort_keys=True)
    return json.loads(text.replace(str(work.resolve()), "/srv/fixture"))


def generate(with_restic: bool | None = None) -> tuple[list[dict], dict]:
    if with_restic is None:
        with_restic = shutil.which("restic") is not None
    with tempfile.TemporaryDirectory(prefix="hs-contract-") as tmp:
        work = Path(tmp)
        examples, openapi = run_scenario(work, with_restic=with_restic)
        return _scrub(examples, work), openapi


def export_contract(out: Path) -> list[Path]:
    examples, openapi = generate()
    out.mkdir(parents=True, exist_ok=True)
    ex_dir = out / "examples"
    if ex_dir.exists():
        shutil.rmtree(ex_dir)
    ex_dir.mkdir()
    written = []
    p = out / "openapi.json"
    p.write_text(json.dumps(openapi, indent=2, sort_keys=True) + "\n")
    written.append(p)
    for i, ex in enumerate(examples, 1):
        p = ex_dir / f"{i:02d}_{ex['name']}.json"
        p.write_text(json.dumps(ex, indent=2, sort_keys=True) + "\n")
        written.append(p)
    return written


def compatible(a, b) -> bool:
    """Shapes match, treating null as compatible with any shape (nullable
    fields) and an empty list as compatible with any list."""
    if a == "null" or b == "null":
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(compatible(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return not a or not b or compatible(a[0], b[0])
    if {a, b} <= {"int", "float"}:
        return True
    return a == b


def shape(value):
    """JSON shape: types of scalars, keys of objects, element shape of lists."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if value is None:
        return "null"
    return type(value).__name__
