#!/usr/bin/env bash
# Stage only. cutover.py is the separately verified, reversible activation.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
MODE="${1:---check}"
SERVICE_USER="${SERVICE_USER:-kiosk}"
REL=/opt/home-services/compat/releases/20261025-retirement
ETC=/etc/home-services/compat
if [ "$MODE" != --apply ]; then
  echo "Stage $ROOT/compat into $REL; private config -> $ETC; user $SERVICE_USER."
  echo "No activation, backup or pruning. Run --apply as root to stage."
  exit 0
fi
[ "$(id -u)" = 0 ] || { echo 'run as root' >&2; exit 1; }
[ ! -e "$REL" ] || { echo "$REL already exists; inspect rather than overwrite" >&2; exit 1; }
/opt/workspace-util/.venv/bin/python "$HERE/install-runtime.py" --source "$ROOT/compat" --destination "$REL"
install -m 0755 "$HERE/network-guard.sh" "$REL/network-guard.sh"
install -m 0644 "$ROOT/src/home_services/observe.py" "$REL/observe.py"
install -m 0755 "$HERE/observe-owned.py" "$REL/observe-owned.py"
install -m 0755 "$HERE/cutover.py" /opt/home-services/compat/cutover.py
install -m 0755 "$HERE/observe.sh" /opt/home-services/compat/observe
install -m 0644 "$HERE/verify-pi.py" /opt/home-services/compat/verify-pi.py
install -m 0644 "$HERE/retention-preview.py" /opt/home-services/compat/retention-preview.py
mkdir -p "$ETC" /var/lib/home-services/compat/logs
chmod 0750 "$ETC"
chgrp "$SERVICE_USER" "$ETC"
install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" /etc/workspace-util/secrets.env "$ETC/secrets.env"
"$REL/venv/bin/python" - <<'PY'
from pathlib import Path
import yaml
source=Path('/etc/workspace-util/config.yaml')
target=Path('/etc/home-services/compat/config.yaml')
if target.exists():
    raise SystemExit('private compatibility config exists; inspect before overwriting')
c=yaml.safe_load(source.read_text().replace('/opt/workspace-util', '/var/lib/home-services/compat'))
c.setdefault('workspace', {}).setdefault('test_workspace', {})['enabled']=False
c.setdefault('camera', {})['background_capture_enabled']=False
target.write_text(yaml.safe_dump(c, sort_keys=False))
PY
chmod 0640 "$ETC/config.yaml"
chown "$SERVICE_USER:$SERVICE_USER" "$ETC/config.yaml"
cp -a /opt/workspace-util/logs/. /var/lib/home-services/compat/logs/
chown -R "$SERVICE_USER:$SERVICE_USER" /var/lib/home-services/compat
for source in "$HERE"/systemd/*; do
  name="$(basename "$source")"
  sed -e "s/^User=kiosk$/User=$SERVICE_USER/" -e "s/^Group=kiosk$/Group=$SERVICE_USER/" "$source" > "/etc/systemd/system/$name"
done
mkdir -p /etc/systemd/system/home-services-observe.service.d
cat > /etc/systemd/system/home-services-observe.service.d/20-owned-services.conf <<'UNIT'
[Service]
ExecStart=
ExecStart=/opt/home-services/compat/observe record
UNIT
ln -s "$REL" /opt/home-services/compat/current
systemctl daemon-reload
"$REL/venv/bin/home-services-compat" --config "$ETC/config.yaml" disk require backup
echo 'Staged and mount checked. No services started. Next: retention dry run, then cutover.py --apply.'
