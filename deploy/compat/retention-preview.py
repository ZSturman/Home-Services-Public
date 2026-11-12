#!/usr/bin/env python3
"""Read-only preview of the inherited policy before moving its scheduler."""
import json
import subprocess

from workspace_util.backup import _restic_env, ensure_backup_disk_mounted
from workspace_util.config import load_config

c = load_config("/etc/home-services/compat/config.yaml")
ensure_backup_disk_mounted(c.disk)
policy = {name: getattr(c.backup, f"{name}_keep") for name in ("hourly", "daily", "weekly", "monthly")}
args = ["restic", "-r", c.backup.repository_path, "forget", "--dry-run", "--no-lock", "--json"]
for name, count in policy.items():
    args.extend([f"--keep-{name}", str(count)])
result = subprocess.run(args, env=_restic_env(c.backup.repository_path), capture_output=True, text=True, check=True)
groups = json.loads(result.stdout)
print(json.dumps({"dry_run": True, "policy": policy,
                  "keep": sum(len(g.get("keep") or []) for g in groups),
                  "remove": sum(len(g.get("remove") or []) for g in groups)}))
