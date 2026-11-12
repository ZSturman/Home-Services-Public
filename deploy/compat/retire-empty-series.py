#!/usr/bin/env python3
"""Retire alerts for a proven empty, no-longer-scheduled source; keep history."""
import argparse
import json
import os
import shutil
from pathlib import Path

from home_services.config import load_config
from home_services.services import build_services

parser = argparse.ArgumentParser()
parser.add_argument("--host", required=True)
parser.add_argument("--tag", required=True)
args = parser.parse_args()
path = Path("/etc/home-services/config.toml")
config = load_config(path)
s = build_services(config)
try:
    groups = [g for g in s.backups.groups() if g["hostname"] == args.host and g["tags"] == [args.tag]]
    if len(groups) != 1 or not groups[0]["protection"].get("empty"):
        raise SystemExit("source is absent/ambiguous/nonempty; refusing to retire it")
finally:
    s.db.close()
if any(r.host == args.host and r.tag == args.tag for r in config.backup.retired_series):
    raise SystemExit("already retired; nothing changed")
backup = path.with_name("config.toml.bak-20261025-retirement")
if backup.exists():
    raise SystemExit("rollback config already exists; inspect instead of overwriting")
shutil.copy2(path, backup)
original = path.stat()
os.chown(backup, original.st_uid, original.st_gid)
text = path.read_text() + (
    "\n# Empty Pi test source retired with its hourly timer; snapshots remain.\n"
    "[[backup.retired_series]]\n"
    f"host = {json.dumps(args.host)}\n"
    f"tag = {json.dumps(args.tag)}\n"
    'reason = "empty test backup retired during Home Services consolidation (October 25, 2026)"\n'
)
temp = path.with_name("config.toml.retirement-new")
shutil.copy2(path, temp)
os.chown(temp, original.st_uid, original.st_gid)
temp.write_text(text)
load_config(temp)  # validate before publishing
temp.replace(path)
print("empty source retired; snapshots preserved; config rollback copy retained")
