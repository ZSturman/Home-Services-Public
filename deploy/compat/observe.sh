#!/usr/bin/env bash
# Include owned compatibility units while the core gateway stays on 0.4.4.
set -euo pipefail
exec /opt/home-services/current/venv/bin/python \
  /opt/home-services/compat/current/observe-owned.py \
  --config /etc/home-services/config.toml observe "$@"
