#!/usr/bin/env python3
"""Storage failure drill on real kernel mounts. Run on the host as root:

    sudo /opt/home-services/current/venv/bin/python storage_drill.py [--user kiosk] [--restore-from SNAPSHOT:PATH]

It builds two disposable sparse exFAT images under /var/tmp/hs-drill, attaches
them with losetup, and runs a separate Home Services instance (port 8791, its
own state directory) whose only writable volume is /mnt/hs-drill. Then it
pulls the disk out from under that instance mid-upload (lazy unmount),
mounts a different disk at the same path, makes the empty mount point
writable, and interrupts restores by volume loss and by killing the worker.
Each scenario prints PASS or FAIL with what was observed.

With --restore-from, the restore scenarios read one archived file from the
live repository (read-only, the configured password file); without it they
are skipped. The live instance, its volume, and the predecessor are never
touched. Everything is removed at the end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = Path("/var/tmp/hs-drill")
MNT = Path("/mnt/hs-drill")
VENV = Path("/opt/home-services/current/venv/bin")
PORT = 8791
URL = f"http://127.0.0.1:{PORT}"
RESULTS: list[tuple[str, bool, str]] = []


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


class Disk:
    def __init__(self, name: str, size_mb: int = 3072):
        self.img = BASE / f"{name}.img"
        with open(self.img, "wb") as f:
            f.truncate(size_mb * 1024 * 1024)  # sparse
        sh("mkfs.exfat", "-L", name[:11], str(self.img))
        self.dev = sh("losetup", "--find", "--show", str(self.img)).stdout.strip()
        sh("udevadm", "settle", check=False)
        self.uuid = sh("blkid", "-p", "-s", "UUID", "-o", "value", self.dev).stdout.strip()

    def mount(self, uid: int, gid: int) -> None:
        sh("mount", "-t", "exfat", "-o", f"uid={uid},gid={gid}", self.dev, str(MNT))

    def detach(self) -> None:
        sh("losetup", "-d", self.dev, check=False)


def mounted() -> bool:
    return sh("mountpoint", "-q", str(MNT), check=False).returncode == 0


def pull_disk() -> None:
    sh("umount", "-l", str(MNT))  # what the kernel does when a USB disk vanishes under open handles


def sd_mountpoint_untouched() -> tuple[bool, str]:
    st = MNT.stat()
    entries = list(MNT.iterdir())
    return (not mounted() and not entries and st.st_uid == 0 and (st.st_mode & 0o222) == 0,
            f"unmounted mount point owner uid {st.st_uid}, mode {oct(st.st_mode & 0o777)}, {len(entries)} entries")


def write_config(uuid: str, restic: bool) -> Path:
    cfg = BASE / "config.toml"
    repo = ""
    if restic:
        repo = '''
[[volumes]]
id = "backup-hdd"
mount = "/mnt/workspace-backup"
uuid = "653E-F09E"
fstype = "exfat"
[volumes.roots]
restic_repository = "restic-repo"
'''
    cfg.write_text(f'''environment = "production"
data_dir = "{BASE}/data"
[network]
port = {PORT}
bind_addresses = ["127.0.0.1"]
[mounts]
probe = "linux"
[[volumes]]
id = "drill"
mount = "{MNT}"
uuid = "{uuid}"
fstype = "exfat"
[volumes.roots]
files = "hs/files"
staging = "hs/staging"
recordings = "hs/recordings"
{repo}
[limits]
volume_reserve_bytes = 50000000
staging_cap_bytes = 600000000
[backup]
enabled = {"true" if restic else "false"}
password_file = "/etc/home-services/secrets/restic-password"
[cameras]
engine = "none"
[notifications]
enabled = false
[legacy_contract]
enabled = false
''')
    return cfg


class Instance:
    def __init__(self, user: str, cfg: Path):
        self.user, self.cfg = user, cfg
        self.procs: dict[str, subprocess.Popen] = {}

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return sh("runuser", "-u", self.user, "--", str(VENV / "home-services"), "--config", str(self.cfg), *args, check=False)

    def start(self, name: str, *args: str) -> None:
        log = open(BASE / f"{name}.log", "ab")
        self.procs[name] = subprocess.Popen(["runuser", "-u", self.user, "--", str(VENV / "home-services"), "--config", str(self.cfg), *args],
                                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def kill(self, name: str, sig=signal.SIGTERM) -> None:
        p = self.procs.pop(name, None)
        if p is None:
            return
        # Signal the whole process group (the worker and its restic), as systemd does to a unit's cgroup.
        os.killpg(p.pid, sig)
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()

    def stop_all(self) -> None:
        for name in list(self.procs):
            self.kill(name)


def wait_health() -> None:
    for _ in range(60):
        try:
            if httpx.get(f"{URL}/healthz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("drill gateway did not start")


def wait_job(api: httpx.Client, job_id: str, states: tuple[str, ...], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = api.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in states or time.monotonic() > deadline:
            return job
        time.sleep(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="kiosk")
    ap.add_argument("--restore-from", help="SNAPSHOT:/abs/path of a large archived file for the restore scenarios")
    args = ap.parse_args()
    if os.geteuid() != 0:
        print("run as root (needs losetup/mount)", file=sys.stderr)
        return 2
    if MNT.exists() and (mounted() or any(MNT.iterdir())):
        print(f"{MNT} is in use; refusing to run", file=sys.stderr)
        return 2
    pw = pwd.getpwnam(args.user)
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True)
    os.chown(BASE, pw.pw_uid, pw.pw_gid)
    MNT.mkdir(exist_ok=True)
    os.chown(MNT, 0, 0)
    os.chmod(MNT, 0o555)
    disk_a, disk_b = Disk("hsdrilla"), Disk("hsdrillb")
    inst = Instance(args.user, write_config(disk_a.uuid, bool(args.restore_from)))
    (BASE / "data").mkdir()
    os.chown(BASE / "data", pw.pw_uid, pw.pw_gid)
    try:
        record("distinct_disks", disk_a.uuid != disk_b.uuid and bool(disk_a.uuid), f"A={disk_a.uuid} B={disk_b.uuid}")
        disk_a.mount(pw.pw_uid, pw.pw_gid)
        prep = inst.cli("volumes", "prepare", "--volume", "drill", "--yes")
        record("prepare_roots", prep.returncode == 0, prep.stdout.strip().replace("\n", "; ") or prep.stderr.strip())
        token = inst.cli("tokens", "create", "--name", "drill", "--preset", "admin").stderr.strip().rsplit(" ", 1)[-1]
        inst.start("gateway", "serve")
        inst.start("worker-backup", "worker", "backup")
        wait_health()
        api = httpx.Client(base_url=URL, headers={"Authorization": f"Bearer {token}"}, timeout=60)

        # 1. Disk pulled mid-upload: writes stop, nothing lands on the SD card, the upload resumes after replug.
        data = os.urandom(40 * 1024 * 1024)
        digest = hashlib.sha256(data).hexdigest()
        up = api.post("/api/v1/transfers/uploads", json={"path": "drill/big.bin", "size": len(data), "sha256": digest}).json()
        chunk = 8 * 1024 * 1024
        for off in (0, chunk):
            api.put(f"/api/v1/transfers/uploads/{up['id']}/content", content=data[off:off + chunk],
                    headers={"Upload-Offset": str(off), "Content-Type": "application/octet-stream"})
        pull_disk()
        r = api.put(f"/api/v1/transfers/uploads/{up['id']}/content", content=data[2 * chunk:3 * chunk],
                    headers={"Upload-Offset": str(2 * chunk), "Content-Type": "application/octet-stream"})
        record("upload_refused_after_unplug", r.status_code == 503 and r.json()["error"]["code"] == "volume_unavailable",
               f"{r.status_code} {r.json()['error']['code']} state={r.json()['error']['details'].get('state')}")
        codes = {name: api.request(m, p, **kw).status_code for name, m, p, kw in (
            ("list", "GET", "/api/v1/storage/files/entries", {}),
            ("mkdir", "POST", "/api/v1/storage/files/directories", {"json": {"path": "x"}}),
            ("new_upload", "POST", "/api/v1/transfers/uploads", {"json": {"path": "y.bin", "size": 10}}),
            ("move", "POST", "/api/v1/storage/files/move", {"json": {"from": "drill", "to": "z"}}))}
        record("all_writes_fail_closed", set(codes.values()) == {503}, json.dumps(codes))
        ok, detail = sd_mountpoint_untouched()
        record("nothing_written_to_sd_card", ok, detail)
        ready = api.get("/api/v1/readiness").json()["modules"]["volume:drill"]
        record("readiness_reports_missing", ready["volume_state"] == "missing", json.dumps(ready))
        doctor = inst.cli("doctor")
        record("doctor_exits_nonzero_when_missing", doctor.returncode == 1, doctor.stdout.splitlines()[3] if doctor.stdout else doctor.stderr)

        # 2. A different disk at the same mount point is refused by UUID.
        disk_b.mount(pw.pw_uid, pw.pw_gid)
        r = api.get("/api/v1/storage/files/entries")
        record("wrong_disk_refused", r.status_code == 503 and r.json()["error"]["details"].get("state") == "wrong_volume",
               f"{r.status_code} {r.json()['error']['details']}")
        r = api.post("/api/v1/storage/files/directories", json={"path": "intruder"})
        record("wrong_disk_not_written", r.status_code == 503 and not any(Path(MNT).iterdir()),
               f"{r.status_code}; disk B entries: {[p.name for p in MNT.iterdir()]}")
        sh("umount", str(MNT))

        # 3. Even a writable empty mount point never receives data: the guard checks identity, not permissions.
        os.chmod(MNT, 0o777)
        doctor = inst.cli("doctor")
        r = api.post("/api/v1/storage/files/directories", json={"path": "leak"})
        leaked = list(MNT.iterdir())
        record("writable_mountpoint_flagged_and_unused", "mountpoint" in doctor.stdout and r.status_code == 503 and not leaked,
               f"doctor flags mount point: {'mountpoint' in doctor.stdout}; mkdir {r.status_code}; entries {leaked}")
        os.chmod(MNT, 0o555)

        # 4. Replug the right disk: the acknowledged offset survived; finish and verify.
        disk_a.mount(pw.pw_uid, pw.pw_gid)
        status = api.get(f"/api/v1/transfers/uploads/{up['id']}").json()
        record("offset_survived_unplug", status["state"] == "open" and status["offset"] == 2 * chunk, f"state={status['state']} offset={status['offset']}")
        off = status["offset"]
        while off < len(data):
            r = api.put(f"/api/v1/transfers/uploads/{up['id']}/content", content=data[off:off + chunk],
                        headers={"Upload-Offset": str(off), "Content-Type": "application/octet-stream"})
            off = int(r.headers["Upload-Offset"])
        done = api.post(f"/api/v1/transfers/uploads/{up['id']}/complete", json={"create_parents": True}).json()
        got = hashlib.sha256(api.get("/api/v1/storage/files/content", params={"path": done["published_path"]}).content).hexdigest()
        record("resume_after_replug_verified", done["state"] == "published" and got == digest, f"{done['state']} sha256 match={got == digest}")

        # 5. Restores interrupted by volume loss and by a killed worker.
        if args.restore_from:
            snap, path = args.restore_from.split(":", 1)
            job = api.post("/api/v1/backups/restores", headers={"Idempotency-Key": "drill-1"},
                           json={"snapshot_id": snap, "include": [path]}).json()
            started = time.monotonic()
            while time.monotonic() - started < 900:
                job = api.get(f"/api/v1/jobs/{job['id']}").json()
                pct = (job["progress"]["data"] or {}).get("percent_done") or 0
                if job["state"] not in ("waiting", "running") or 0.1 <= pct <= 0.7:
                    break
                time.sleep(0.2)
            was = f"{job['state']} at {pct:.0%}"
            pull_disk()
            job = wait_job(api, job["id"], ("failed", "succeeded", "interrupted"), 120)
            record("restore_stops_on_volume_loss", job["state"] == "failed" and (job["error"] or {}).get("code") == "volume_unavailable",
                   f"disk pulled while {was}; then state={job['state']} error={(job['error'] or {}).get('code')}")
            ok, detail = sd_mountpoint_untouched()
            record("restore_wrote_nothing_to_sd_card", ok, detail)
            disk_a.mount(pw.pw_uid, pw.pw_gid)

            job = api.post("/api/v1/backups/restores", headers={"Idempotency-Key": "drill-2"},
                           json={"snapshot_id": snap, "include": [path]}).json()
            while api.get(f"/api/v1/jobs/{job['id']}").json()["state"] == "waiting":
                time.sleep(0.1)
            was = api.get(f"/api/v1/jobs/{job['id']}").json()["state"]
            inst.kill("worker-backup", signal.SIGKILL)
            inst.start("worker-backup", "worker", "backup")  # what systemd's Restart=always does
            job = wait_job(api, job["id"], ("interrupted", "failed", "succeeded"), 200)
            record("killed_worker_job_becomes_interrupted", job["state"] == "interrupted" and job["error"]["code"] == "lease_expired",
                   f"worker killed while {was}; then state={job['state']} error={(job['error'] or {}).get('code')}")
            job = api.post("/api/v1/backups/restores", headers={"Idempotency-Key": "drill-3"},
                           json={"snapshot_id": snap, "include": [path]}).json()
            job = wait_job(api, job["id"], ("succeeded", "failed", "interrupted"), 900)
            archived = api.get(f"/api/v1/backups/snapshots/{snap}/content", params={"path": path})
            restored = api.get("/api/v1/storage/files/content", params={"path": f"{job['result']['destination']['path']}{path}"})
            same = hashlib.sha256(archived.content).hexdigest() == hashlib.sha256(restored.content).hexdigest()
            record("restore_after_recovery_matches_archive", job["state"] == "succeeded" and same,
                   f"state={job['state']} bytes={len(restored.content)} sha256 match={same}")
        api.close()
    finally:
        inst.stop_all()
        if mounted():
            sh("umount", "-l", str(MNT), check=False)
        disk_a.detach()
        disk_b.detach()
        shutil.rmtree(BASE, ignore_errors=True)
        try:
            MNT.rmdir()
        except OSError:
            pass
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
