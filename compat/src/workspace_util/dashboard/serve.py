"""Entry point for running the dashboard as a service.

Resolves the bind host from config (auto-detecting the Tailscale IP when
left blank) rather than hardcoding 0.0.0.0, so the dashboard never becomes
reachable from anywhere but the tailnet without an explicit config change.
"""
from __future__ import annotations

import argparse
import os

import uvicorn

from ..config import load_config
from ..net import resolve_bind_host


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    if args.config:
        os.environ["WORKSPACE_CONFIG_FILE"] = args.config

    cfg = load_config(args.config)
    host = resolve_bind_host(cfg.dashboard.bind_host)

    from . import app as app_module
    uvicorn.run(app_module.app, host=host, port=cfg.dashboard.port, log_level="info")


if __name__ == "__main__":
    main()
