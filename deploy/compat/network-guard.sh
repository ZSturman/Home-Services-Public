#!/usr/bin/env bash
# Self-healing check for the one failure mode that has actually happened on
# pi.example.local: after a reboot, NetworkManager brings wlan0 up as an available,
# managed device but never attempts to auto-activate the saved Wi-Fi profile
# -- it just sits idle (confirmed via `journalctl -u NetworkManager`: zero
# log lines for wlan0 between boot and a manual `nmcli` reconnect). Runs
# every few minutes via home-services-network-guard.timer so a repeat of that
# never again requires someone to walk up and reconnect Wi-Fi by hand.
#
# Idempotent and cheap: when wlan0 is already connected with a working
# default route and tailscaled is healthy, this does nothing but exit 0.
set -uo pipefail

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S%z')] $*"; }

WIFI_DEVICE="wlan0"

wifi_state() {
    nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: -v d="$WIFI_DEVICE" '$1 == d { print $2 }'
}

has_default_route() {
    ip route show default 2>/dev/null | grep -q "^default"
}

internet_reachable() {
    # A couple of independent, stable endpoints so one outage doesn't cause
    # a false "still down" escalation.
    curl -fsS --max-time 5 -o /dev/null https://1.1.1.1 2>/dev/null && return 0
    curl -fsS --max-time 5 -o /dev/null https://8.8.8.8 2>/dev/null && return 0
    return 1
}

tailscale_healthy() {
    command -v tailscale >/dev/null 2>&1 || return 0 # nothing to check
    local backend
    # Pretty-printed JSON puts a space after the colon, so match that
    # loosely rather than assuming a specific `tailscale` JSON formatting.
    backend=$(tailscale status --json 2>/dev/null | grep -o '"BackendState": *"[^"]*"' | cut -d'"' -f4)
    [[ "$backend" == "Running" ]]
}

state="$(wifi_state)"
log "wlan0 state: ${state:-unknown}"

if [[ "$state" != "connected" ]]; then
    log "wlan0 not connected -- asking NetworkManager to connect its best known network"
    sudo -n nmcli device connect "$WIFI_DEVICE" >/dev/null 2>&1
    sleep 5
    state="$(wifi_state)"
    log "wlan0 state after reconnect attempt: ${state:-unknown}"
fi

if [[ "$state" == "connected" ]] && ! has_default_route; then
    log "wlan0 connected but no default route -- cycling the interface"
    sudo -n nmcli device disconnect "$WIFI_DEVICE" >/dev/null 2>&1
    sleep 2
    sudo -n nmcli device connect "$WIFI_DEVICE" >/dev/null 2>&1
    sleep 5
fi

if [[ "$(wifi_state)" != "connected" ]]; then
    log "wlan0 still not connected after a direct reconnect -- restarting NetworkManager"
    sudo -n systemctl restart NetworkManager
    sleep 10
    log "wlan0 state after NetworkManager restart: $(wifi_state)"
fi

if has_default_route && ! internet_reachable; then
    log "have a default route but no internet reachability -- leaving as-is for the next run (transient upstream outage, not a Pi-side fix)"
fi

if ! tailscale_healthy; then
    log "tailscaled backend not Running -- restarting tailscaled"
    sudo -n systemctl restart tailscaled
fi

log "done"
