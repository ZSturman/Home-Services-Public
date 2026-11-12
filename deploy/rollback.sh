#!/usr/bin/env bash
# Point /opt/home-services/current at a previous release. Does not restart
# services unless --restart is given; never touches config, data, or disks.
#
# Usage: deploy/rollback.sh [--prefix /] [--to RELEASE_ID] [--restart]
set -euo pipefail
PREFIX=/
TO=""
RESTART=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --to) TO="$2"; shift 2 ;;
    --restart) RESTART=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
BASE="${PREFIX%/}/opt/home-services"
CURRENT="$(basename "$(readlink "$BASE/current")")"
if [ -z "$TO" ]; then
  TO="$(ls -1 "$BASE/releases" | sort | grep -v -x "$CURRENT" | tail -n 1 || true)"
fi
[ -n "$TO" ] && [ -d "$BASE/releases/$TO" ] || { echo "no previous release to roll back to" >&2; exit 1; }
ln -sfn "$BASE/releases/$TO" "$BASE/current.new"
if mv --version >/dev/null 2>&1; then
  mv -Tf "$BASE/current.new" "$BASE/current"   # GNU: atomic rename over the old link
else
  rm -f "$BASE/current.new"; ln -sfhn "$BASE/releases/$TO" "$BASE/current"   # BSD (development only)
fi
echo "current: $CURRENT -> $TO"
if [ "$RESTART" -eq 1 ]; then
  # Restart only enabled units, so a rollback never starts something that was off.
  for unit in home-services-gateway.service home-services-notifier.service \
      home-services-worker@storage.service home-services-worker@backup.service home-services-worker@camera.service; do
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then systemctl restart "$unit"; fi
  done
fi
