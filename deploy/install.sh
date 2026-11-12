#!/usr/bin/env bash
# Install a Home Services release under <prefix>/opt/home-services/releases/.
#
# Dry run by default: prints the plan and changes nothing. With --apply it
# creates the release venv from the hash-pinned lock plus the built wheel,
# installs units and an example config (never overwriting an existing config),
# and switches the `current` symlink atomically. It never enables, starts, or
# stops any service; it never touches disks, the restic repository, or the
# predecessor's /opt/workspace-util and workspace-* units.
#
# --service-user names the account the units run as (default home-services).
# On an exFAT volume only the mount owner (fstab uid=) can write, so the Pi
# uses its existing mount owner instead of changing fstab ownership.
#
# Usage: deploy/install.sh --wheel dist/home_services-0.1.0-py3-none-any.whl [--prefix /] [--python python3]
#          [--service-user home-services] [--apply]
set -euo pipefail

PREFIX=/
PYTHON=python3
WHEEL=""
APPLY=0
SERVICE_USER=home-services
HERE="$(cd "$(dirname "$0")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --wheel) WHEEL="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --service-user) SERVICE_USER="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$WHEEL" ] && [ -f "$WHEEL" ] || { echo "--wheel <path to built wheel> is required" >&2; exit 2; }

ROOT="${PREFIX%/}"
VERSION="$(basename "$WHEEL" | cut -d- -f2)"
RELEASE_ID="${VERSION}-$(date -u +%Y%m%dT%H%M%SZ)"
BASE="$ROOT/opt/home-services"
REL="$BASE/releases/$RELEASE_ID"
ETC="$ROOT/etc/home-services"
DATA="$ROOT/var/lib/home-services"
UNITS="$ROOT/etc/systemd/system"

cat <<PLAN
release:   $REL
python:    $("$PYTHON" -c 'import sys; print(sys.version.split()[0])')
config:    $ETC/config.toml ($( [ -f "$ETC/config.toml" ] && echo "kept" || echo "example installed; edit before starting" ))
data:      $DATA
units:     $UNITS/home-services-{gateway,notifier}.service, home-services-worker@.service
user:      $SERVICE_USER (units run as this account; owns $DATA and $ETC/secrets)
current -> $REL
PLAN

if [ "$APPLY" -ne 1 ]; then
  echo "dry run: re-run with --apply to install (services are never started by this script)"
  exit 0
fi

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python >= 3.11 required" >&2; exit 1; }
[ -e "$REL" ] && { echo "$REL already exists" >&2; exit 1; }
mkdir -p "$BASE/releases" "$ETC/secrets" "$DATA" "$UNITS"
"$PYTHON" -m venv "$REL/venv"
"$REL/venv/bin/pip" install --quiet --require-hashes -r "$HERE/requirements.lock"
"$REL/venv/bin/pip" install --quiet --no-deps "$WHEEL"
cp "$HERE/requirements.lock" "$REL/"
echo "$RELEASE_ID" > "$REL/RELEASE"
"$REL/venv/bin/home-services" --help >/dev/null

install -m 0755 "$HERE/rollback.sh" "$BASE/rollback.sh"   # stable path for operators
[ -f "$ETC/config.toml" ] || install -m 0640 "$HERE/config.example.toml" "$ETC/config.toml"
chmod 0700 "$ETC/secrets"
for unit in home-services-gateway.service home-services-notifier.service home-services-worker@.service \
    home-services-observe.service home-services-observe.timer; do
  install -m 0644 "$HERE/systemd/$unit" "$UNITS/$unit"
  if [ "$SERVICE_USER" != home-services ] && [ "${unit%.timer}" = "$unit" ]; then
    mkdir -p "$UNITS/$unit.d"
    printf '[Service]\nUser=%s\nGroup=%s\n' "$SERVICE_USER" "$SERVICE_USER" > "$UNITS/$unit.d/10-service-user.conf"
  fi
done
for dropin in "$HERE"/systemd/*.service.d/*.conf; do
  [ -e "$dropin" ] || continue
  dir="$UNITS/$(basename "$(dirname "$dropin")")"
  mkdir -p "$dir"
  install -m 0644 "$dropin" "$dir/$(basename "$dropin")"
done
if [ "$ROOT" = "" ] && id "$SERVICE_USER" >/dev/null 2>&1; then
  chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA" "$ETC/secrets"
  chgrp "$SERVICE_USER" "$ETC/config.toml"
fi

ln -sfn "$REL" "$BASE/current.new"
if mv --version >/dev/null 2>&1; then
  mv -Tf "$BASE/current.new" "$BASE/current"   # GNU: atomic rename over the old link
else
  rm -f "$BASE/current.new"; ln -sfhn "$REL" "$BASE/current"   # BSD (development only)
fi
echo "installed $RELEASE_ID; current -> $(readlink "$BASE/current")"
echo "next (owner-run): review $ETC/config.toml, then see docs/INSTALL.md for user, ownership, token, and start commands"
