#!/usr/bin/env python3
"""Camera notification drill: motion push and "camera not recording", end to
end through ntfy, on the host, without touching the real camera. Run as root:

    sudo /opt/home-services/current/venv/bin/python camera_notify_drill.py --user kiosk

A separate Home Services instance (own state, port 8792) records a synthetic
live HLS stream (FFmpeg lavfi, never a device) onto a disposable loop-mounted
exFAT image, with the drill camera armed and notifications going to a fresh
random ntfy.sh topic used only by this drill. Checks: a "Motion: ..." message
reaches ntfy for the motion window; after the stream stops, "Camera not
recording" reaches ntfy after the monitor's grace period. Everything is
removed at the end. Takes about 6 minutes.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = Path("/var/tmp/hs-cam-drill")
MNT = Path("/mnt/hs-cam-drill")
VENV = Path("/opt/home-services/current/venv/bin")


def sh(*a, check=True):
    return subprocess.run(a, check=check, capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="kiosk")
    args = ap.parse_args()
    if os.geteuid() != 0:
        print("run as root", file=sys.stderr)
        return 2
    pw = pwd.getpwnam(args.user)
    shutil.rmtree(BASE, ignore_errors=True)
    for d in (BASE, BASE / "data", BASE / "live", BASE / "secrets"):
        d.mkdir(parents=True)
        os.chown(d, pw.pw_uid, pw.pw_gid)
    os.chmod(BASE / "secrets", 0o700)
    MNT.mkdir(exist_ok=True)
    img = BASE / "disk.img"
    with open(img, "wb") as f:
        f.truncate(512 * 1024 * 1024)
    sh("mkfs.exfat", str(img))
    dev = sh("losetup", "--find", "--show", str(img)).stdout.strip()
    procs: list[subprocess.Popen] = []
    results = []
    topic = f"hs-drill-{secrets.token_hex(12)}"
    try:
        sh("udevadm", "settle", check=False)
        uuid = sh("blkid", "-p", "-s", "UUID", "-o", "value", dev).stdout.strip()
        sh("mount", "-t", "exfat", "-o", f"uid={pw.pw_uid},gid={pw.pw_gid}", dev, str(MNT))
        url_file = BASE / "secrets/ntfy-url"
        url_file.write_text(f"https://ntfy.sh/{topic}\n")
        os.chown(url_file, pw.pw_uid, pw.pw_gid)
        os.chmod(url_file, 0o600)
        cfg = BASE / "config.toml"
        cfg.write_text(f'''environment = "production"
data_dir = "{BASE}/data"
[network]
port = 8792
bind_addresses = ["127.0.0.1"]
[[volumes]]
id = "drill"
mount = "{MNT}"
uuid = "{uuid}"
fstype = "exfat"
[volumes.roots]
files = "hs/files"
staging = "hs/staging"
recordings = "hs/recordings"
[limits]
volume_reserve_bytes = 10000000
[backup]
enabled = false
[cameras]
engine = "hls-recorder"
notification_cooldown_seconds = 0
event_quiet_seconds = 5
[[cameras.devices]]
id = "drill-cam"
name = "Drill camera"
source = "{BASE}/live/index.m3u8"
[notifications]
enabled = true
ntfy_url_file = "{url_file}"
[legacy_contract]
enabled = false
''')
        hs = ["runuser", "-u", args.user, "--", str(VENV / "home-services"), "--config", str(cfg)]
        sh(*hs, "volumes", "prepare", "--volume", "drill", "--yes")
        token = sh(*hs, "tokens", "create", "--name", "drill", "--preset", "camera-monitor").stderr.strip().rsplit(" ", 1)[-1]
        # The stream: noise, then a moving square from 20 s to 35 s (after the recorder's 10 s warm-up), 60 s total.
        bg = "color=c=0x404040:s=640x360:r=10:d=60,noise=alls=3:allf=t"
        graph = (f"{bg}[bg];color=c=white:s=90x90:r=10:d=60[box];"
                 "[bg][box]overlay=x='mod((t-20)*640,550)':y=135:enable='between(t,20,35)'")
        publisher = subprocess.Popen(["runuser", "-u", args.user, "--", "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                                      "-f", "lavfi", "-i", graph, "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
                                      "-g", "20", "-f", "hls", "-hls_time", "2", "-hls_list_size", "6", "-hls_flags",
                                      "delete_segments+independent_segments", str(BASE / "live/index.m3u8")])
        procs.append(publisher)
        time.sleep(3)
        for argv in (["serve"], ["worker", "camera"], ["notifier"]):
            procs.append(subprocess.Popen(hs + argv, stdout=open(BASE / f"{argv[0]}.log", "ab"), stderr=subprocess.STDOUT))
        for _ in range(60):  # arm the drill camera through its own API
            try:
                r = httpx.put("http://127.0.0.1:8792/api/v1/cameras/drill-cam/armed", json={"armed": True},
                              headers={"Authorization": f"Bearer {token}"}, timeout=5)
                if r.status_code == 200 and r.json()["armed"]:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("could not arm the drill camera")

        def topic_titles() -> list[str]:
            r = httpx.get(f"https://ntfy.sh/{topic}/json", params={"poll": "1", "since": "all"}, timeout=20)
            return [json.loads(line).get("title", "") for line in r.text.splitlines() if line.strip()]

        deadline = time.time() + 90
        while time.time() < deadline and not any(t.startswith("Motion:") for t in topic_titles()):
            time.sleep(5)
        titles = topic_titles()
        ok = any(t == "Motion: Drill camera" for t in titles)
        results.append(("motion_notification_delivered", ok, titles))
        print(f"[{'PASS' if ok else 'FAIL'}] motion notification delivered to ntfy: {titles}", flush=True)
        publisher.wait(timeout=120)  # the 60 s clip ends: the feed goes stale
        deadline = time.time() + 330  # 15 s staleness + 180 s grace + 60 s monitor tick + delivery
        while time.time() < deadline and "Camera not recording" not in topic_titles():
            time.sleep(10)
        titles = topic_titles()
        ok = "Camera not recording" in titles
        results.append(("camera_not_recording_delivered", ok, titles))
        print(f"[{'PASS' if ok else 'FAIL'}] 'Camera not recording' delivered to ntfy: {titles}", flush=True)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    p.kill()
        sh("umount", "-l", str(MNT), check=False)
        sh("losetup", "-d", dev, check=False)
        shutil.rmtree(BASE, ignore_errors=True)
        try:
            MNT.rmdir()
        except OSError:
            pass
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
