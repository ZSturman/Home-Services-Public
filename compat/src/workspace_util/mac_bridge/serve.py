"""Entry point for running the Mac companion as a LaunchAgent."""
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
    host = resolve_bind_host("")  # Mac bridge always binds its own Tailscale IP

    from . import app as app_module
    uvicorn.run(app_module.app, host=host, port=cfg.mac_bridge.port, log_level="info")


if __name__ == "__main__":
    main()
