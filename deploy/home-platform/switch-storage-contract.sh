#!/usr/bin/env bash
# Point Home Platform's storage/backup status at Home Services instead of
# the predecessor dashboard, or switch it back. Run on the Pi as the user
# that owns the Home Platform deployment (kiosk).
#
#   switch-storage-contract.sh            dry run: show current settings and check reachability
#   switch-storage-contract.sh --apply    back up .env and the token file, mint a home-platform-legacy
#                                         token into the token file, set the URL, recreate control-plane-api
#   switch-storage-contract.sh --revert   restore the newest backups and recreate control-plane-api
#
# Only the control-plane-api container reads these settings; no other
# Home Platform service, file, or database is touched. The token is written
# straight into the secret file and never printed.
set -euo pipefail
HP="${HOME_PLATFORM_DIR:-/home/service-user/home-platform/home-platform-next}"
HS_URL="${HOME_SERVICES_URL:-https://$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'):8443}"
HS="/opt/home-services/current/venv/bin/home-services --config /etc/home-services/config.toml"
ENV="$HP/.env"
SECRET="$HP/secrets/workspace_contract_token"
MODE="${1:-check}"

cd "$HP"
current="$(grep -E '^WORKSPACE_CONTRACT_URL=' "$ENV" | cut -d= -f2-)"
echo "Home Platform: $HP"
echo "current WORKSPACE_CONTRACT_URL: $current"
echo "Home Services: $HS_URL"

probe() {  # from inside the container, as Home Platform will see it (no token needed for healthz)
  docker compose exec -T control-plane-api python -c "import urllib.request,sys; print(urllib.request.urlopen(sys.argv[1], timeout=10).status)" "$1/healthz"
}

case "$MODE" in
  check|--check)
    echo "Home Services /healthz from control-plane-api: $(probe "$HS_URL")"
    echo "dry run: --apply to switch, --revert to switch back"
    ;;
  --apply)
    [ "$(probe "$HS_URL")" = 200 ] || { echo "Home Services is not reachable from control-plane-api; not switching" >&2; exit 1; }
    stamp="$(date +%Y%m%d%H%M%S)"
    cp -p "$ENV" "$ENV.bak-hs-$stamp"
    cp -p "$SECRET" "$SECRET.bak-hs-$stamp"
    token="$($HS tokens create --name "Home Platform (control-plane-api)" --preset home-platform-legacy 2>&1 >/dev/null | sed -n 's/.*: \(hst_[^ ]*\)$/\1/p')"
    [ -n "$token" ] || { echo "could not create a token; nothing changed" >&2; exit 1; }
    (umask 077; printf '%s' "$token" > "$SECRET.new") && mv "$SECRET.new" "$SECRET"
    unset token
    sed -i "s#^WORKSPACE_CONTRACT_URL=.*#WORKSPACE_CONTRACT_URL=$HS_URL/api/storage/contract#" "$ENV"
    docker compose up -d --no-deps control-plane-api
    echo "switched; backups: $ENV.bak-hs-$stamp, $SECRET.bak-hs-$stamp"
    echo "verify in the Home Platform dashboard (Files: workspace mode, backup, backup disk), then revoke the old"
    echo "predecessor dashboard token only after the predecessor is retired"
    ;;
  --revert)
    env_bak="$(ls -1t "$ENV".bak-hs-* 2>/dev/null | head -1)"
    sec_bak="$(ls -1t "$SECRET".bak-hs-* 2>/dev/null | head -1)"
    [ -n "$env_bak" ] && [ -n "$sec_bak" ] || { echo "no backups to restore" >&2; exit 1; }
    cp -p "$env_bak" "$ENV"
    cp -p "$sec_bak" "$SECRET"
    docker compose up -d --no-deps control-plane-api
    echo "restored $env_bak and $sec_bak; revoke the Home Services token with: $HS tokens list / tokens revoke <id>"
    ;;
  *) echo "usage: $0 [--apply|--revert]" >&2; exit 2 ;;
esac
