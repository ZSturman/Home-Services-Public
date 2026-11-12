#!/usr/bin/env python3
"""Bootstrap an independent runtime using a known-good environment on this host.

No network or editable predecessor install is retained. Run with the old
environment's Python; venv records its system interpreter, not the old venv.
For subsequent releases use pip install compat/ in a normal isolated venv.
"""
import argparse
import json
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    dest = args.destination.resolve()
    if dest.exists():
        parser.error(f"destination exists: {dest}; choose a new release")
    old_site = Path(sysconfig.get_paths()["purelib"])
    subprocess.run([sys.executable, "-m", "venv", str(dest / "venv")], check=True)
    python = dest / "venv/bin/python"
    new_site = Path(subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True,
    ).strip())
    for entry in old_site.iterdir():
        name = entry.name.lower()
        if name.startswith(("__editable__", "workspace_util", "home_services_compat")):
            continue
        if name.endswith(".pth"):
            # Only preserve setuptools' standard hook, never old source paths.
            if entry.name != "distutils-precedence.pth":
                continue
        if name == "__pycache__":
            continue
        target = new_site / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True, symlinks=False)
        else:
            shutil.copy2(entry, target)
    shutil.copytree(args.source / "src", dest / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(args.source / "pyproject.toml", dest / "pyproject.toml")
    (new_site / "home_services_compat.pth").write_text(str(dest / "src") + "\n")
    executable = dest / "venv/bin/home-services-compat"
    executable.write_text(f'#!/bin/sh\nexec "{python}" -m workspace_util.cli "$@"\n')
    executable.chmod(0o755)
    dependencies = subprocess.check_output([
        str(python), "-c",
        "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()},sort_keys=True))",
    ], text=True)
    (dest / "DEPENDENCIES.json").write_text(dependencies)
    subprocess.run([str(python), "-c", "import workspace_util.camera_stream, workspace_util.dashboard.app, workspace_util.mac_bridge.app"], check=True)
    print(json.dumps({"runtime": str(dest), "imports": "ok"}))


if __name__ == "__main__":
    main()
