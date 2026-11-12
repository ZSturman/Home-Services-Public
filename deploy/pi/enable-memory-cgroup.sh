#!/usr/bin/env bash
# Enable the kernel memory cgroup controller on a Raspberry Pi so systemd
# MemoryMax= limits (and Docker memory limits) are enforced.
#
# The Pi firmware prepends `cgroup_disable=memory` to the kernel command
# line; a later `cgroup_enable=memory` from cmdline.txt overrides it. This
# script only edits /boot/firmware/cmdline.txt (after a dated backup) and
# NEVER reboots. The change takes effect at the next reboot the owner
# schedules. Dry run unless --apply.
#
# Before applying, it lists every memory limit that would start being
# enforced (all containers and units) so nothing is surprised by it.
# Revert: restore the printed backup file (or remove the word) and reboot.
#
# Usage: sudo deploy/pi/enable-memory-cgroup.sh [--apply]
set -euo pipefail
CMDLINE=/boot/firmware/cmdline.txt
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

[ -f "$CMDLINE" ] || { echo "$CMDLINE not found (not a Raspberry Pi?)" >&2; exit 2; }
[ "$(wc -l < "$CMDLINE")" -le 1 ] || { echo "$CMDLINE has more than one line; refusing to edit" >&2; exit 2; }

echo "current kernel: $(tr ' ' '\n' < /proc/cmdline | grep -E '^cgroup_(en|dis)able=' | tr '\n' ' ')"
if grep -qw "cgroup_enable=memory" "$CMDLINE"; then
  echo "cmdline.txt already contains cgroup_enable=memory (active after the next reboot if not already)"
  exit 0
fi

echo "limits that would start being enforced:"
for u in $(systemctl list-units --type=service --no-legend --plain | awk '{print $1}'); do
  v="$(systemctl show "$u" -p MemoryMax -p MemoryHigh --value | tr '\n' ' ')"
  case "$v" in *[0-9]*) echo "  unit $u: MemoryMax/High $v" ;; esac
done
if command -v docker >/dev/null; then
  for c in $(docker ps -q); do
    docker inspect "$c" --format '  container {{.Name}}: limit={{.HostConfig.Memory}} (0 = none)'
  done
fi

NEW="$(tr -d '\n' < "$CMDLINE") cgroup_enable=memory"
echo "new cmdline.txt: $NEW"
if [ "$APPLY" -ne 1 ]; then
  echo "dry run: re-run with --apply to write it (no reboot is performed)"
  exit 0
fi
BACKUP="$CMDLINE.bak-$(date +%Y%m%d%H%M%S)"
cp -p "$CMDLINE" "$BACKUP"
printf '%s\n' "$NEW" > "$CMDLINE.new"
[ "$(wc -l < "$CMDLINE.new")" -eq 1 ] && grep -q "root=" "$CMDLINE.new" || { rm -f "$CMDLINE.new"; echo "validation failed; unchanged" >&2; exit 1; }
mv "$CMDLINE.new" "$CMDLINE"
sync
echo "written; backup at $BACKUP. Takes effect at the next reboot."
echo "after rebooting, verify: grep memory /sys/fs/cgroup/cgroup.controllers"
