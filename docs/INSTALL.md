# Install, operate, troubleshoot, and roll back

## Article relay (0.4.5 / contract 1.3)

The optional Content Manager article module needs only an additive gateway upgrade. Use `deploy/ship.sh <ssh-target> --service-user <service-user> --gateway-only` to preserve running storage/backup/camera workers. It creates its own private `article-relay.sqlite3`, uses separate phone/companion token presets and never starts a publisher. See ARTICLE_SYNC.md for protocol, quotas, client setup and SQLite recovery.

## Migrated compatibility services (October 25, 2026)

Home Services now owns the USB camera producer, compatibility dashboard,
read-only backup mount, daily retention, weekly check, disk/network guards,
and the Mac command bridge. These use a separate runtime and keep existing
URLs, credentials and recovery paths. They do not run from an archived checkout.

See [INTEGRATIONS.md](INTEGRATIONS.md) for every app/service data flow and
MIGRATION.md for installed paths, exact checks and rollback.
`deploy/ship.sh` updates the core API/workers; it does **not** update the
compatibility runtime or the Mac bridge. `deploy/compat/` owns their separate
bootstrap/cutover lifecycle. Never enable competing old and new camera/backup
services. Retention and check keep their existing schedule and repository;
no new retention policy or repository initialization is part of retirement.


Supported hosts: Linux arm64 or x86_64 with Python ≥ 3.11, systemd, `restic`
for backup features, and `ffmpeg` for the camera engine and exports.

The Pi `pi.example.local` (Raspberry Pi 5, 4 GB, Debian 13, Python 3.13.5, restic
0.18.0, ffmpeg) was installed on October 20, 2026 with the steps below.

## Development (any machine, isolated fixtures)

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/home-services --config config/development.toml dev-fixtures --with-restic
.venv/bin/home-services --config config/development.toml doctor
.venv/bin/home-services --config config/development.toml tokens create --name dev --preset files-backups
.venv/bin/home-services --config config/development.toml serve            # 127.0.0.1:8790
.venv/bin/home-services --config config/development.toml worker storage   # separate terminals
.venv/bin/home-services --config config/development.toml worker backup
.venv/bin/python -m pytest
```

`dev-fixtures` creates everything under the ignored `.dev/`: a stand-in
volume, a fixture mount table that claims UUID `DEV-BACKUP-0001`, managed
roots, one synthetic legacy camera event, and, with `--with-restic`, a
disposable fixture repository. To simulate unplugging, delete the volume's
entry from `.dev/mounts.json`; to simulate the wrong disk, change its `uuid`.
The fixture probe is refused unless `environment = "development"`.

## Deploying updates (from the development Mac)

```sh
deploy/ship.sh service-user@pi.example.local --service-user kiosk
```

`ship.sh` builds the wheel, copies it and `deploy/` to the host, installs a
new release under `/opt/home-services/releases/<version>-<UTC stamp>`,
switches `current`, restarts the enabled Home Services units, waits for
`/healthz`, and runs `doctor`. If the new release is not healthy within 20
seconds, it points `current` back at the previous release and restarts on
it. Config, state, disks, and the predecessor are never touched.

`deploy/requirements.lock` pins every runtime dependency by hash
(`uv export --format requirements-txt --no-dev --no-emit-project --python 3.11 -o deploy/requirements.lock`);
it installs cleanly on Python 3.13/arm64. Hosts never need a source checkout.

## First installation on the Pi (as performed)

Read-only checks first. Results on pi.example.local:

```sh
findmnt -no SOURCE,FSTYPE,OPTIONS /mnt/workspace-backup
#   /dev/sda2 exfat rw,relatime,uid=1000,gid=1000,fmask=0022,dmask=0022,...
sudo blkid -s UUID -o value /dev/sda2          # 653E-F09E (fstab: UUID=653E-F09E ... nofail)
test -f /mnt/workspace-backup/restic-repo/config && echo "repository present"
sudo unshare -m sh -c 'mkdir -p /run/r && mount --bind / /run/r && stat -c "%a %U:%G" /run/r/mnt/workspace-backup'
#   755 root:root   (the empty mount point is not writable by the service user)
```

**Service account.** exFAT has no per-file ownership: only the mount owner
(`uid=1000`, `kiosk`) can write, and changing the fstab ownership is an owner
decision. The units therefore run as `kiosk` (the predecessor's account) via
drop-ins written by `--service-user`. The repository and legacy camera
media are protected by Home Services' code (read-only roles and path
containment), not by filesystem permissions.

```sh
sudo bash deploy/install.sh --wheel home_services-<v>-py3-none-any.whl --service-user kiosk            # plan
sudo bash deploy/install.sh --wheel home_services-<v>-py3-none-any.whl --service-user kiosk --apply
sudoedit /etc/home-services/config.toml      # uuid = "653E-F09E", companion_repository, cameras (below)
umask 077; sudo sh -c '. /etc/workspace-util/secrets.env; printf %s "$WORKSPACE_BACKUP_PASSWORD" > /etc/home-services/secrets/restic-password'
sudo chown kiosk:kiosk /etc/home-services/secrets/restic-password && sudo chmod 600 /etc/home-services/secrets/restic-password
HS="/opt/home-services/current/venv/bin/home-services --config /etc/home-services/config.toml"
$HS volumes prepare --volume backup-hdd          # dry run, then --yes: creates only home-services/{files,staging,recordings}
$HS doctor
sudo systemctl daemon-reload
sudo systemctl enable --now home-services-gateway home-services-worker@storage home-services-worker@backup
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8790
```

The repository password is the same one the predecessor uses (copied, never
printed). Home Services has no code path that initializes a repository.

### Notifications (ntfy)

```sh
umask 077; echo "https://ntfy.sh/hs-$(openssl rand -hex 16)" > /etc/home-services/secrets/ntfy-url   # as kiosk
# config.toml: [notifications] enabled = true
sudo systemctl enable --now home-services-notifier
```

The topic is the only credential for reading the notifications, so it is
random and private (also kept in the Mac Keychain as
`home-services-ntfy-topic`). Subscribe to it in the ntfy iPhone app. Messages
contain volume IDs, host names, tags, job IDs, and camera names only: never
paths, URLs, tokens, or passwords. HTTP client request logging is suppressed
so the topic never reaches the journal. To rotate: write a new URL, restart
the notifier, resubscribe.

### Camera recorder

```toml
[cameras]
engine = "hls-recorder"
retention_days = 30
continuous_retention_hours = 168  # 7 days without motion
[[cameras.devices]]
id = "workspace-pi-camera"
name = "Workspace Pi Camera"
kind = "usb"
source = "/run/workspace-camera-hls/index.m3u8"   # written by workspace-camera-stream (predecessor); read only
```

```sh
sudo systemctl enable --now home-services-worker@camera
```

The recorder never opens `/dev/video0`. It needs `ffmpeg` and read access to
the playlist directory (`kiosk`). Its drop-in sets `Nice=5`, I/O priority 3,
`CPUQuota=60%`, and `MemoryMax=300M`. The backup worker runs at `Nice=10`,
I/O priority 7.

**Memory limits.** The Pi firmware adds `cgroup_disable=memory` to the
kernel command line, so `MemoryMax` is not enforced today (the recorder uses
about 55 MB). Enabling the controller is safe on pi.example.local: no Docker
container has a memory limit and the recorder's 300 MB is the only unit
limit, so nothing running would start being limited (checked October 20).
It needs one edit and a reboot the owner schedules:

```sh
sudo deploy/pi/enable-memory-cgroup.sh            # dry run: lists every limit that would apply
sudo deploy/pi/enable-memory-cgroup.sh --apply    # appends cgroup_enable=memory to cmdline.txt (backup kept); no reboot
# at the next planned reboot, then: grep memory /sys/fs/cgroup/cgroup.controllers
```

Revert: restore the printed `cmdline.txt.bak-*` and reboot. A malformed
`cmdline.txt` can stop the Pi from booting; the script refuses multi-line
files and validates the result before replacing it.

### Retention preview (read-only)

```sh
$HS backups retention-preview            # keep rules from [backup.retention] in the config
$HS backups retention-preview --keep-hourly 24 --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --json
```

Lists, per host+paths group, the snapshots a `restic forget` with those
keep rules would remove. It reimplements restic's keep rules over a
`--no-lock` snapshot listing: no `forget`, no `prune`, no lock. Checked
against `restic forget --dry-run --no-lock` (restic 0.18 on the Pi's
repository, October 21: identical keep/remove sets, 48 snapshots, 5 to
remove; and 0.19 on generated repositories in `tests/test_retention.py`).
The predecessor's policy is 24 hourly, 14 daily, 8 weekly, 12 monthly.

### Observation log

```sh
sudo systemctl enable --now home-services-observe.timer      # every 15 minutes
$HS observe report --hours 48                                # exit 1 if anything needs attention
```

Each sample (`/var/lib/home-services/observation/<date>.jsonl`, kept 30
days) records Home Services and predecessor unit states and restart counts,
doctor results, readiness, camera recording coverage and motion events for
the interval, backup series ages, notification failures, failed jobs, and
memory/load/temperature/disk. No paths or secrets.

### Tokens

```sh
$HS tokens create --name "Files & Backups (iPhone)" --preset files-backups [--ttl-days 90]
$HS tokens create --name "Camera Monitor (iPhone)" --preset camera-monitor
$HS tokens create --name "Home Platform" --preset home-platform-legacy
$HS tokens list
$HS tokens revoke <id>       # effective on the next request
```

Each token is printed once (stderr); only its SHA-256 is stored. On the Mac,
tokens issued during deployment live in the login Keychain (account
`home-services`, services `home-services-admin-token`,
`home-services-token-files-backups`, `home-services-token-companion`,
`home-services-token-camera-monitor`).

## What runs where (pi.example.local)

| Unit | Purpose | Notes |
| --- | --- | --- |
| `home-services-gateway` | API on 127.0.0.1:8790 | `Restart=always`; exits 75 to rebind if a bound interface changes |
| `home-services-worker@storage` | upload cleanup, Trash purge (hourly) | |
| `home-services-worker@backup` | restore jobs | `Nice=10`; SIGTERM marks running restores `interrupted` |
| `home-services-worker@camera` | HLS recorder, motion, retention | limits above |
| `home-services-notifier` | health monitor + ntfy delivery | |
| `home-services-observe.timer` | observation sample every 15 min | oneshot; `observe report` summarizes |
| Tailscale Serve `https:8443` | tailnet-only HTTPS → 127.0.0.1:8790 | `/` (predecessor dashboard) and TCP 445 (backup SMB view) unchanged |

Ports listening because of Home Services: only 127.0.0.1:8790. Nothing binds
a wildcard or public address; `tailscale funnel` is not used.

Storage: state in `/var/lib/home-services` (about 90 MB, mostly restic's
index cache); data under `/mnt/workspace-backup/home-services/`; camera
continuous recording is about 15 GB/day before retention.

## Diagnostics and troubleshooting

```sh
$HS doctor                       # exits 1 on any error-level check; --json available
$HS volumes status
journalctl -u home-services-gateway -u 'home-services-worker@*' -u home-services-notifier -n 100 --no-pager
curl -s https://pi.example.local.<tailnet>.ts.net:8443/healthz                                  # {"status":"ok"}
curl -s -H "Authorization: Bearer $TOKEN" https://pi.example.local.<tailnet>.ts.net:8443/api/v1/readiness
```

| Symptom | Meaning and action |
| --- | --- |
| `503 volume_unavailable`, `state: missing` | the HDD is not mounted. Nothing was written elsewhere. Remount (`sudo mount /mnt/workspace-backup`); uploads resume from their acknowledged offset. |
| `state: wrong_volume` | a different filesystem is mounted at the mount point; check `blkid`. Never edit the configured UUID to "fix" this. |
| doctor: `volume:…:mountpoint` error | the unmounted mount point is writable; while unmounted: `sudo chown root:root /mnt/workspace-backup && sudo chmod 0755 /mnt/workspace-backup`. Writes are still refused, but fix it. |
| readiness `backups: unavailable` | repository missing or password file absent; `doctor` says which. |
| snapshot calls return `502 restic_failed` | restic error text is in `error.details.stderr` (no secrets). Wrong password shows "wrong password or no key found". |
| camera `recorder_unavailable` | `systemctl status home-services-worker@camera` |
| camera `stale` / `offline` | the predecessor stream stopped: `systemctl status workspace-camera-stream` |
| outbox `failed` messages | `GET /api/v1/notifications/outbox` shows `last_error` (never the URL); check outbound HTTPS to ntfy.sh. |
| restore `failed` with `volume_unavailable` | the disk went away during the restore; partial data is in `Restores/<job>`; run a new restore. |

Logs never include tokens, the ntfy URL, the repository password,
media-session secrets (`/api/v1/media/sessions/<redacted>`), or query strings
(file paths are passed as query parameters and are not logged). Verified by
searching the journal after the live acceptance run.

## Testing against the deployed host

For optional recovery of current Mac companion snapshots on iPhone/iPad, see
mobile recovery. It adds a separate guarded recovery root and
private password file; the companion remains the backup writer.

```sh
HOME_SERVICES_LIVE_URL=https://pi.example.local.<tailnet>.ts.net:8443 \
HOME_SERVICES_LIVE_TOKEN=$(security find-generic-password -s home-services-admin-token -w) \
HOME_SERVICES_LIVE_SSH=service-user@pi.example.local .venv/bin/python -m pytest tests/live -v
# storage failure drill on loop-mounted disposable exFAT images (root on the host):
scp deploy/drills/storage_drill.py service-user@pi.example.local:/tmp/ && ssh service-user@pi.example.local \
  'sudo /opt/home-services/current/venv/bin/python /tmp/storage_drill.py --user kiosk --restore-from <snapshot>:<large file>'
```

The live suite writes only inside `files/_acceptance/<run>/` and
`files/Restores/<job>/` and moves them to managed Trash; the drill uses
`/var/tmp/hs-drill` and `/mnt/hs-drill` and removes both.

## Rollback

```sh
ls /opt/home-services/releases/
sudo /opt/home-services/rollback.sh                       # point current at the previous release
sudo /opt/home-services/rollback.sh --to 0.2.0-20261020T191423Z --restart   # restarts only enabled units
```

Rollback changes only the `current` link. Releases are kept side by side in
`/opt/home-services/releases/`. The database schema only ever adds tables, so
an older release runs on a newer database unless the schema version is
higher than it knows (it then refuses to start rather than guess).

To remove Home Services entirely (the predecessor needs no restore step,
because none of its units, files, or settings were changed):

```sh
sudo systemctl disable --now home-services-gateway home-services-notifier \
  home-services-worker@storage home-services-worker@backup home-services-worker@camera
sudo tailscale serve --https=8443 off
```

Managed files, recordings, and Trash remain on the disk under
`/mnt/workspace-backup/home-services/`; delete that folder only deliberately.
