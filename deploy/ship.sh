#!/usr/bin/env bash
# Build this checkout and install it on a host that already has Home Services
# configured (see docs/INSTALL.md). Run from the repository root on the
# development machine.
#
# Steps: build the wheel, copy it and deploy/ to the host, install a new
# release (install.sh --apply), restart the Home Services units, then check
# /healthz and `doctor`. If the health check fails, `current` is pointed back
# at the previous release and the units are restarted on it. Config, state,
# disks, and the predecessor's services are never touched.
#
# Usage: deploy/ship.sh <ssh-target> [--service-user kiosk]
set -euo pipefail
TARGET="${1:?usage: deploy/ship.sh <ssh-target> [--service-user USER]}"
shift
SERVICE_USER=home-services
GATEWAY_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --service-user) SERVICE_USER="$2"; shift 2 ;;
    --gateway-only) GATEWAY_ONLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

rm -rf dist
uv build --wheel -q
WHEEL="$(ls dist/home_services-*.whl)"
STAGE="/tmp/home-services-ship"
ssh "$TARGET" "rm -rf $STAGE && mkdir -p $STAGE"
scp -q -r deploy "$WHEEL" "$TARGET:$STAGE/"

ssh "$TARGET" "STAGE=$STAGE WHEEL=$(basename "$WHEEL") SERVICE_USER=$SERVICE_USER GATEWAY_ONLY=$GATEWAY_ONLY bash -s" <<'REMOTE'
set -euo pipefail
cd "$STAGE"
UNITS="home-services-gateway.service"
if [ "$GATEWAY_ONLY" != 1 ]; then
  UNITS="$UNITS home-services-worker@storage.service home-services-worker@backup.service"
  systemctl is-enabled --quiet home-services-notifier.service 2>/dev/null && UNITS="$UNITS home-services-notifier.service"
  systemctl is-enabled --quiet home-services-worker@camera.service 2>/dev/null && UNITS="$UNITS home-services-worker@camera.service"
fi
PREVIOUS="$(basename "$(readlink /opt/home-services/current)")"
sudo bash deploy/install.sh --wheel "$WHEEL" --service-user "$SERVICE_USER" --apply | tail -n 2
sudo systemctl daemon-reload
sudo systemctl restart $UNITS
healthy=0
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8790/healthz >/dev/null 2>&1 && systemctl is-active --quiet $UNITS; then healthy=1; break; fi
  sleep 1
done
if [ "$healthy" -ne 1 ]; then
  echo "new release is not healthy; rolling back to $PREVIOUS" >&2
  sudo journalctl -u home-services-gateway -n 20 --no-pager >&2 || true
  if [ "$GATEWAY_ONLY" = 1 ]; then
    sudo bash deploy/rollback.sh --to "$PREVIOUS"
    sudo systemctl restart home-services-gateway.service
  else
    sudo bash deploy/rollback.sh --to "$PREVIOUS" --restart
  fi
  exit 1
fi
sudo -u "$SERVICE_USER" /opt/home-services/current/venv/bin/home-services --config /etc/home-services/config.toml doctor || true
echo "deployed $(cat /opt/home-services/current/RELEASE) (previous: $PREVIOUS)"
REMOTE
