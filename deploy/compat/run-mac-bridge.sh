#!/bin/bash
# Runtime/config/secrets are installed outside all source checkouts.
set -euo pipefail
BASE="$HOME/Library/Application Support/HomeServices/MacBridge"
if [[ -f "$BASE/secrets.env" ]]; then
  set -a
  source "$BASE/secrets.env"
  set +a
fi
exec "$BASE/current/venv/bin/python" -m workspace_util.mac_bridge.serve --config "$BASE/config.yaml"
