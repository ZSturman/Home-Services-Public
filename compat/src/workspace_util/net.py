"""Small networking helpers -- resolving the Tailscale interface address so
services bind to it specifically instead of 0.0.0.0 (design principle:
Tailscale-only access for dashboard/API/SSH, never exposed more broadly)."""
from __future__ import annotations

import json
import shutil
import subprocess

# LaunchAgents/systemd services often run with a minimal PATH that omits
# Homebrew's bin dirs (a real deployment hit exactly this: the Mac bridge
# silently bound to 127.0.0.1 instead of its Tailscale IP because
# `subprocess.run(["tailscale", ...])` raised FileNotFoundError under
# launchd's default PATH). Fall back to known install locations rather than
# depending on the caller's PATH being right.
_TAILSCALE_FALLBACK_PATHS = [
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/usr/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
]


def _tailscale_binary() -> str:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in _TAILSCALE_FALLBACK_PATHS:
        if shutil.which(candidate):
            return candidate
    return "tailscale"  # let subprocess raise FileNotFoundError as before


def tailscale_ipv4() -> str | None:
    try:
        out = subprocess.run([_tailscale_binary(), "ip", "-4"], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        ip = out.stdout.strip()
        return ip or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def tailscale_status() -> dict | None:
    try:
        out = subprocess.run([_tailscale_binary(), "status", "--json"], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def resolve_bind_host(configured: str) -> str:
    """Empty config value means "auto-detect the Tailscale IP"; anything
    explicit is used as-is (so tests / local dev can still bind 127.0.0.1)."""
    if configured:
        return configured
    ip = tailscale_ipv4()
    if ip:
        return ip
    return "127.0.0.1"  # safe fallback: loopback-only, never silently public
