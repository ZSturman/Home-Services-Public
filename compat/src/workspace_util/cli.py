"""`workspace` CLI -- see docs/architecture.md for the full command list."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import click

from . import backup as backup_mod
from . import disk as disk_mod
from .commands import CommandHistory, CommandNotAllowedError, run_whitelisted_command
from .config import Config, ConfigError, load_config
from .doctor import format_report, run_doctor
from .git_scan import scan_repo
from .logs import SUBSYSTEMS, tail_log
from .mount_safety import MountSafetyError, check_mount
from .projects import discover_projects, human_age


def _load(ctx) -> Config:
    try:
        return load_config(ctx.obj.get("config_path"))
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)


@click.group()
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.pass_context
def main(ctx, config_path):
    """Workspace Sync & Backup Utility."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@main.command()
@click.pass_context
def status(ctx):
    """Quick one-screen status summary."""
    cfg = _load(ctx)
    results = run_doctor(cfg)
    click.echo(format_report(results))


@main.command()
@click.pass_context
def doctor(ctx):
    """Full health check across every subsystem."""
    cfg = _load(ctx)
    results = run_doctor(cfg)
    click.echo(format_report(results))
    from .doctor import overall_status
    sys.exit(0 if overall_status(results) != "UNHEALTHY" else 1)


@main.group()
def storage():
    """The stable storage-state contract other projects should consult."""


@storage.command("contract")
@click.pass_context
def storage_contract_cmd(ctx):
    """Print the storage contract as JSON -- the CLI mirror of the
    dashboard's GET /api/storage/contract, for anyone on the Pi itself
    (e.g. another project's deploy/ops scripts) who'd rather shell out than
    make an HTTP call."""
    from .workspace_state import build_storage_contract
    cfg = _load(ctx)
    click.echo(json.dumps(build_storage_contract(cfg).as_dict(), indent=2))


@main.group()
def sync():
    """Syncthing status/control."""


@sync.command("status")
@click.pass_context
def sync_status(ctx):
    cfg = _load(ctx)
    from .syncthing import SyncthingClient, SyncthingError
    try:
        client = SyncthingClient.from_local_config()
    except SyncthingError as exc:
        click.echo(f"Syncthing not available: {exc}", err=True)
        sys.exit(1)
    if not client.ping():
        click.echo("Syncthing installed but not responding", err=True)
        sys.exit(1)
    conns = client.connections()
    for device_id, info in conns.get("connections", {}).items():
        click.echo(f"{device_id[:7]}...: {'connected' if info.get('connected') else 'disconnected'}")


@sync.command("restart")
def sync_restart():
    """Restart the Syncthing service (systemd on Pi, brew services on Mac)."""
    import platform
    import subprocess
    if platform.system() == "Linux":
        subprocess.run(["sudo", "systemctl", "restart", "syncthing@$(whoami)"], shell=False)
    else:
        subprocess.run(["brew", "services", "restart", "syncthing"])
    click.echo("Restart requested.")


@main.group()
def camera():
    """Camera capture, motion, and Mac sync status/control."""


@camera.command("status")
@click.pass_context
def camera_status(ctx):
    """Capture/storage/motion/sync status -- the CLI mirror of the
    dashboard's camera card, useful for `ssh`-only troubleshooting."""
    cfg = _load(ctx)
    from . import camera as camera_mod
    from . import camera_sync
    from .camera_service import load_out_of_house

    status = camera_mod.status(cfg.camera)
    click.echo(f"Camera enabled: {status['enabled']}  available: {status['available']}  ({status['detail']})")
    if not cfg.camera.enabled:
        return
    usage = camera_mod.storage_usage(cfg.camera).as_dict()
    click.echo(
        f"Storage: {usage['snapshot_count']} snapshots, {usage['event_count']} events, "
        f"{usage['total_bytes'] / 1e6:.1f} MB used, "
        f"{(usage['filesystem_free_bytes'] or 0) / 1e9:.1f} GB free on that filesystem"
        + (" -- LOW SPACE" if usage["low_space_warning"] else "")
    )
    armed = load_out_of_house(cfg.logging.dir, cfg.camera.out_of_house_default)
    click.echo(f"Out of the House: {'ARMED' if armed else 'disarmed'}")
    if cfg.camera.sync_enabled:
        sync_status = camera_sync.folder_sync_status(cfg)
        click.echo(f"Mac sync: {sync_status}")


@camera.command("sync-setup-pi")
@click.argument("mac_device_id")
@click.pass_context
def camera_sync_setup_pi(ctx, mac_device_id):
    """Run once on the Pi: create the send-only "camera" Syncthing folder
    paired with the Mac's device ID (see docs/camera.md#mac-sync)."""
    cfg = _load(ctx)
    from .camera import ensure_storage_dir
    from .camera_sync import setup_pi_side
    from .syncthing import SyncthingClient
    ensure_storage_dir(cfg.camera)
    client = SyncthingClient.from_local_config()
    setup_pi_side(client, mac_device_id, cfg.camera.storage_dir)
    click.echo(f"Camera folder configured (paused) at {cfg.camera.storage_dir}. "
               "Run this again after the backup HDD is mounted if it was skipped.")


@camera.command("sync-setup-mac")
@click.argument("pi_device_id")
@click.argument("mac_sync_path")
@click.pass_context
def camera_sync_setup_mac(ctx, pi_device_id, mac_sync_path):
    """Run once on the Mac: create the receive-only "camera" Syncthing folder."""
    from .camera_sync import setup_mac_side
    from .syncthing import SyncthingClient
    Path(mac_sync_path).mkdir(parents=True, exist_ok=True)
    client = SyncthingClient.from_local_config()
    setup_mac_side(client, pi_device_id, mac_sync_path)
    click.echo(f"Camera folder configured (paused) at {mac_sync_path}.")


@camera.command("sync-resume")
@click.pass_context
def camera_sync_resume(ctx):
    """Unpause the Pi-side camera folder once storage_dir is confirmed
    present (mount-gated, same guard as the Workspace folder)."""
    cfg = _load(ctx)
    from .camera_sync import resume_if_storage_ready
    from .syncthing import SyncthingClient
    client = SyncthingClient.from_local_config()
    resumed = resume_if_storage_ready(client, cfg.camera.storage_dir)
    click.echo("Resumed." if resumed else f"Not resumed: {cfg.camera.storage_dir} is not a real directory yet.")


@main.group()
def disk():
    """Disk discovery and mount status."""


@disk.command("list")
def disk_list():
    for d in disk_mod.list_disks():
        click.echo(
            f"{d.device:<14} {d.model:<20} {d.human(d.capacity_bytes):>8} "
            f"{d.filesystem or '-':<8} {d.mountpoint or '-':<28} {d.uuid or '-'}"
        )


@disk.command("status")
@click.pass_context
def disk_status(ctx):
    cfg = _load(ctx)
    import platform
    from .workspace_state import WorkspaceMode, resolve_workspace_state

    click.echo(f"1. Pi/controller:  operational ({platform.node()})")

    backup_result = check_mount(cfg.disk.backup_mount, required_uuid=cfg.disk.backup_uuid or None)
    backup_state = "MOUNTED and healthy" if backup_result.mounted else f"NOT MOUNTED ({backup_result.reason})"
    click.echo(f"2. Backup HDD:     {backup_state} -- {cfg.disk.backup_mount}")

    ws_state = resolve_workspace_state(cfg)
    if ws_state.mode is WorkspaceMode.PRODUCTION:
        click.echo(f"3. Workspace disk: INSTALLED -- {ws_state.detail}")
        click.echo("4. Workspace sync: eligible to run against the live SSD/NVMe")
    elif ws_state.mode is WorkspaceMode.TEST:
        click.echo(f"3. Workspace disk: NOT INSTALLED (test mode active) -- {ws_state.detail}")
        click.echo("4. Workspace sync: running in TEST MODE only, not production data")
    else:
        click.echo(f"3. Workspace disk: NOT INSTALLED -- {ws_state.detail}")
        click.echo("4. Workspace sync: PENDING -- attach an SSD/NVMe and run scripts/disk-setup.sh workspace")


@disk.command("activate")
@click.argument("which", type=click.Choice(["workspace", "backup"]))
@click.option("--uuid", required=True, help="UUID printed by scripts/disk-setup.sh after mounting")
@click.pass_context
def disk_activate(ctx, which, uuid):
    """Turn a freshly mounted SSD/NVMe (see scripts/disk-setup.sh) into the
    active workspace/backup disk: verifies it's actually mounted with this
    UUID right now, then writes disk.<which>_uuid into config.yaml. Fails
    closed and touches nothing if the disk isn't really mounted -- this
    command is the only supported way to flip a UUID from blank to real,
    turning "attach the SSD" into one short, validated procedure instead of
    a hand-edit. It does not restart the dashboard or resume Syncthing for
    you -- run `workspace dashboard restart` and `workspace sync restart`
    (or reboot) afterward once you've confirmed the new config looks right."""
    from .config import set_disk_uuid

    cfg = _load(ctx)
    mount_point = cfg.disk.workspace_mount if which == "workspace" else cfg.disk.backup_mount
    status = check_mount(mount_point, required_uuid=uuid)
    if not status.mounted:
        click.echo(f"Refusing to activate: {mount_point} is not currently mounted with UUID {uuid} ({status.reason})", err=True)
        sys.exit(1)
    set_disk_uuid(cfg.source_path, which, uuid)
    click.echo(f"disk.{which}_uuid set to {uuid!r} in {cfg.source_path}.")
    click.echo("Next: `workspace dashboard restart` (Pi) so the new config takes effect, "
               "then `workspace doctor` to confirm PRODUCTION mode.")


@disk.command("require")
@click.argument("which", type=click.Choice(["workspace", "backup"]))
@click.pass_context
def disk_require(ctx, which):
    """Exit non-zero with a loud message if the named disk isn't mounted.
    Used as a systemd ExecStart gate -- see services/pi/*.service."""
    cfg = _load(ctx)
    path, uuid, label = (
        (cfg.disk.workspace_mount, cfg.disk.workspace_uuid, "workspace SSD")
        if which == "workspace" else
        (cfg.disk.backup_mount, cfg.disk.backup_uuid, "backup HDD")
    )
    try:
        from .mount_safety import require_mount
        require_mount(path, required_uuid=uuid or None, what=label)
    except MountSafetyError as exc:
        click.echo(f"MOUNT GUARD FAILED: {exc}", err=True)
        sys.exit(1)
    click.echo(f"{label} mounted at {path}")


@main.group()
def backup():
    """Backup status/control (restic-backed)."""


@backup.command("init")
@click.pass_context
def backup_init(ctx):
    """One-time: initialize the restic repository on the backup disk."""
    cfg = _load(ctx)
    if backup_mod.repository_exists(cfg.backup, cfg.disk):
        click.echo("Repository already initialized.")
        return
    try:
        result = backup_mod.init_repository(cfg.backup, cfg.disk)
    except backup_mod.BackupBlockedError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
    if result.returncode == 0:
        click.echo(result.stdout)
    else:
        click.echo(f"restic init FAILED:\n{result.stderr}", err=True)
        sys.exit(1)


@backup.command("status")
@click.pass_context
def backup_status(ctx):
    cfg = _load(ctx)
    if not backup_mod.repository_exists(cfg.backup, cfg.disk):
        click.echo("No backup repository initialized (or backup disk not mounted).")
        return
    snaps = backup_mod.list_snapshots(cfg.backup, cfg.disk)
    click.echo(f"{len(snaps)} snapshot(s)")
    if snaps:
        latest = max(snaps, key=lambda s: s["time"])
        click.echo(f"Last snapshot: {latest['time']} ({latest['short_id']}) tags={latest.get('tags')}")


@backup.command("now")
@click.option("--path", "explicit_path", default=None, help="Back up this path instead of the resolved active workspace (e.g. a small test dataset)")
@click.pass_context
def backup_now(ctx, explicit_path):
    cfg = _load(ctx)
    if explicit_path:
        source = explicit_path
    else:
        from .workspace_state import resolve_workspace_state
        state = resolve_workspace_state(cfg)
        if not state.is_active:
            click.echo(f"Nothing to back up: {state.detail}", err=True)
            sys.exit(2)
        source = state.active_path
    result = backup_mod.run_backup(
        source, cfg.backup, cfg.disk, tags=["manual"],
        status_path=backup_mod.backup_status_path(cfg.logging.dir),
    )
    if result.success:
        click.echo(f"Backup succeeded: snapshot {result.snapshot_id}, {result.data_added_bytes} bytes added")
    else:
        click.echo(f"Backup FAILED: {result.error}", err=True)
        sys.exit(1)


@backup.command("retention")
@click.pass_context
def backup_retention(ctx):
    """Apply the configured hourly/daily/weekly/monthly retention (restic forget --prune)."""
    cfg = _load(ctx)
    try:
        result = backup_mod.apply_retention(cfg.backup, cfg.disk)
    except backup_mod.BackupBlockedError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
    click.echo(result.stdout)
    if result.returncode != 0:
        click.echo(result.stderr, err=True)
        sys.exit(1)


@backup.command("snapshots")
@click.pass_context
def backup_snapshots(ctx):
    cfg = _load(ctx)
    try:
        for s in backup_mod.list_snapshots(cfg.backup, cfg.disk):
            click.echo(f"{s['short_id']}  {s['time']}  tags={s.get('tags')}  paths={s.get('paths')}")
    except backup_mod.BackupBlockedError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)


@backup.command("verify")
@click.option("--read-data", is_flag=True, help="Also verify pack file contents, not just structure")
@click.pass_context
def backup_verify(ctx, read_data):
    cfg = _load(ctx)
    try:
        result = backup_mod.verify_integrity(cfg.backup, cfg.disk, read_data=read_data)
    except backup_mod.BackupBlockedError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
    marker = Path(cfg.logging.dir) / "last_integrity_check.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"timestamp": time.time(), "success": result.returncode == 0}))
    if result.returncode == 0:
        click.echo("Integrity check passed.")
    else:
        click.echo(f"Integrity check FAILED:\n{result.stderr}", err=True)
        sys.exit(1)


@main.group()
def restore():
    """Restore snapshots into a safe, separate directory -- never overwrites the live workspace."""


@restore.command("list")
@click.pass_context
def restore_list(ctx):
    cfg = _load(ctx)
    try:
        for s in backup_mod.list_snapshots(cfg.backup, cfg.disk):
            click.echo(f"{s['short_id']}  {s['time']}")
    except backup_mod.BackupBlockedError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)


@restore.command("run")
@click.argument("snapshot_id")
@click.argument("project", required=False)
@click.option("--restore-root", default="/mnt/workspace-backup/restores", help="Safe destination root -- never the live workspace. Defaults to a subdirectory of the backup disk (ample space); override with a dedicated restore volume if you add one later.")
@click.pass_context
def restore_run(ctx, snapshot_id, project, restore_root):
    cfg = _load(ctx)
    include = None
    if project:
        from .workspace_state import resolve_workspace_state
        state = resolve_workspace_state(cfg)
        base = state.active_path or cfg.workspace.pi_path
        include = str(Path(base) / cfg.workspace.projects_subdir / project)
    try:
        result = backup_mod.restore_snapshot(snapshot_id, cfg.backup, cfg.disk, restore_root, include_path=include)
    except backup_mod.BackupBlockedError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
    if result.returncode == 0:
        click.echo(f"Restored {snapshot_id} into {restore_root}/{time.strftime('%Y-%m-%d')}")
    else:
        click.echo(f"Restore FAILED:\n{result.stderr}", err=True)
        sys.exit(1)


@main.command("projects")
@click.pass_context
def projects_cmd(ctx):
    cfg = _load(ctx)
    from .workspace_state import resolve_workspace_state
    state = resolve_workspace_state(cfg)
    if not state.is_active:
        click.echo(f"No active workspace: {state.detail}")
        return
    root = Path(state.active_path) / cfg.workspace.projects_subdir
    for p in discover_projects(root):
        git = p.git
        state = "not a git repo"
        if git.is_repo:
            state = "clean" if git.clean else f"{git.modified_count} modified, {git.untracked_count} untracked"
        click.echo(f"{p.name:<30} branch={git.branch or '-':<20} {state:<30} changed {human_age(p.last_modified)}")


@main.command("project")
@click.argument("name")
@click.pass_context
def project_cmd(ctx, name):
    cfg = _load(ctx)
    from .workspace_state import resolve_workspace_state
    ws_state = resolve_workspace_state(cfg)
    if not ws_state.is_active:
        click.echo(f"No active workspace: {ws_state.detail}", err=True)
        sys.exit(1)
    path = Path(ws_state.active_path) / cfg.workspace.projects_subdir / name
    if not path.is_dir():
        click.echo(f"No such project: {name}", err=True)
        sys.exit(1)
    git = scan_repo(path)
    click.echo(f"Path: {path}")
    click.echo(f"Branch: {git.branch or '-'}")
    click.echo(f"Clean: {git.clean}")
    click.echo(f"Modified: {git.modified_count}  Untracked: {git.untracked_count}")
    if git.ahead is not None:
        click.echo(f"Ahead/behind: {git.ahead}/{git.behind}")
    for w in git.warnings:
        click.echo(f"WARNING: {w}")


@main.group()
def dashboard():
    """Dashboard service status/control."""


@dashboard.command("status")
@click.pass_context
def dashboard_status(ctx):
    cfg = _load(ctx)
    import httpx
    try:
        r = httpx.get(f"http://{cfg.dashboard.bind_host}:{cfg.dashboard.port}/api/health", timeout=5)
        click.echo(f"Dashboard: {'OK' if r.status_code == 200 else 'unexpected status ' + str(r.status_code)}")
    except Exception as exc:
        click.echo(f"Dashboard unreachable: {exc}", err=True)
        sys.exit(1)


@dashboard.command("restart")
def dashboard_restart():
    import subprocess
    subprocess.run(["sudo", "systemctl", "restart", "home-services-compat-dashboard.service"])
    click.echo("Restart requested.")


@main.group()
def mac():
    """Mac companion status."""


@mac.command("status")
@click.pass_context
def mac_status(ctx):
    cfg = _load(ctx)
    import httpx
    try:
        r = httpx.get(f"http://{cfg.mac_bridge.host}:{cfg.mac_bridge.port}/health", timeout=5)
        click.echo(r.json() if r.status_code == 200 else f"status {r.status_code}")
    except Exception as exc:
        click.echo(f"Mac unreachable: {exc}", err=True)
        sys.exit(1)


@main.group("logs")
def logs_cmd():
    """Tail structured per-subsystem logs."""


for _subsystem in SUBSYSTEMS:
    def _make(subsystem):
        @click.command(subsystem, help=f"Tail the {subsystem} log")
        @click.option("-n", "--lines", default=50)
        @click.pass_context
        def _tail(ctx, lines, subsystem=subsystem):
            cfg = _load(ctx)
            for line in tail_log(subsystem, cfg.logging.dir, lines):
                click.echo(line)
        return _tail
    logs_cmd.add_command(_make(_subsystem))


@main.group("run")
def run_cmd():
    """Run a whitelisted command (see config.commands)."""


@run_cmd.command("list")
@click.pass_context
def run_list(ctx):
    cfg = _load(ctx)
    for name in cfg.commands:
        click.echo(name)


@run_cmd.command("exec")
@click.argument("command_name")
@click.argument("cwd")
@click.pass_context
def run_exec(ctx, command_name, cwd):
    cfg = _load(ctx)
    spec = cfg.commands.get(command_name)
    if not spec:
        click.echo(f"Unknown command {command_name!r}. Try `workspace run list`.", err=True)
        sys.exit(1)
    try:
        result = run_whitelisted_command(spec, cwd)
    except CommandNotAllowedError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
    history = CommandHistory(Path(cfg.logging.dir) / "command_history.jsonl")
    history.record(result)
    click.echo(result.stdout)
    if result.stderr:
        click.echo(result.stderr, err=True)
    sys.exit(result.exit_code if result.exit_code is not None else 1)


if __name__ == "__main__":
    main()
