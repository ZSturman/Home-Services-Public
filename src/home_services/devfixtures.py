"""Isolated development fixtures under an ignored directory (default `.dev/`).

Creates a stand-in volume directory, a fixture mount table that marks it as
"mounted with UUID DEV-BACKUP-0001", managed roots, one synthetic legacy
camera event, and optionally a disposable restic repository with one
snapshot. Nothing outside the fixture root is touched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

DEV_UUID = "DEV-BACKUP-0001"
DEV_PASSWORD = "development-only-password"
LEGACY_EVENT = "20260910T123045-7d79a4"


def create_dev_fixtures(root: Path, *, with_restic: bool = False) -> dict:
    root = root.resolve()
    mount = root / "volumes" / "backup-hdd"
    for rel in ("home-services/files", "home-services/staging", "home-services/recordings", "restic-repo", "camera/events"):
        (mount / rel).mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    table = root / "mounts.json"
    table.write_text(json.dumps({str(mount): {"uuid": DEV_UUID, "fstype": "exfat", "device": "/dev/fixture1"},
                                 "__data_dir_fstype__": {"fstype": "apfs"}}, indent=2))
    secrets = root / "secrets"
    secrets.mkdir(exist_ok=True)
    os.chmod(secrets, 0o700)
    pw = secrets / "restic-password"
    pw.write_text(DEV_PASSWORD + "\n")
    os.chmod(pw, 0o600)

    event = mount / "camera" / "events" / LEGACY_EVENT
    event.mkdir(parents=True, exist_ok=True)
    (event / "meta.json").write_text(json.dumps({
        "event_id": LEGACY_EVENT, "start_ts": "2026-09-10T12:30:45Z", "last_motion_ts": "2026-09-10T12:31:02Z",
        "end_ts": "2026-09-10T12:31:22Z", "camera_device_name": "Fixture Camera", "recording_started": True,
        "recording_path": "recording.mp4", "pre_context": ["pre-00.jpg"], "synced": True, "notified": True}))
    (event / "pre-00.jpg").write_bytes(b"\xff\xd8\xff\xe0fixture-jpeg\xff\xd9")
    (event / "recording.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)

    result = {"root": str(root), "mount": str(mount), "uuid": DEV_UUID, "mount_table": str(table), "restic": "skipped"}
    repo = mount / "restic-repo"
    if with_restic and shutil.which("restic") and not (repo / "config").exists():
        env = {**os.environ, "RESTIC_REPOSITORY": str(repo), "RESTIC_PASSWORD_FILE": str(pw), "RESTIC_CACHE_DIR": str(root / "restic-cache")}
        src = root / "backup-source" / "Projects" / "Demo"
        src.mkdir(parents=True, exist_ok=True)
        (src / "README.md").write_text("fixture project\n")
        (src / "notes.txt").write_text("hello from a fixture snapshot\n")
        subprocess.run(["restic", "init"], env=env, check=True, capture_output=True)
        subprocess.run(["restic", "backup", str(root / "backup-source"), "--tag", "mac-workspace", "--host", "fixture-mac"],
                       env=env, check=True, capture_output=True)
        result["restic"] = "initialized disposable fixture repository"
    elif (repo / "config").exists():
        result["restic"] = "fixture repository present"
    return result
