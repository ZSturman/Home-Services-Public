"""Configuration loading for the Workspace Sync & Backup Utility.

Single source of truth: a YAML file (see config/config.example.yaml). Values
can be overridden with environment variables shaped like
WORKSPACE_<SECTION>__<KEY>, e.g. WORKSPACE_DASHBOARD__PORT=9000.

No secrets are ever read from this file -- secret-shaped keys (token, key,
secret, password) are rejected at load time if a literal value is present, to
stop credentials from accidentally being committed.
"""
from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_LOCATIONS = [
    Path.cwd() / "config" / "config.yaml",
    Path.home() / ".config" / "workspace-util" / "config.yaml",
    Path("/etc/home-services/compat/config.yaml"),
]

ENV_PREFIX = "WORKSPACE_"
_SECRET_KEY_PATTERN = re.compile(r"(token|secret|password|api[_-]?key|auth[_-]?key)$", re.I)


class ConfigError(ValueError):
    pass


def _check_no_literal_secrets(data: Any, path: str = "") -> None:
    """Refuse to load a config file that appears to embed a real secret value."""
    if isinstance(data, dict):
        for key, value in data.items():
            key_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and _SECRET_KEY_PATTERN.search(key):
                if isinstance(value, str) and value and not value.startswith("$") and "env" not in key.lower():
                    raise ConfigError(
                        f"Refusing to load config: '{key_path}' looks like a secret "
                        "with a literal value. Use a *_env_var key pointing at an "
                        "environment variable instead (see docs/security.md)."
                    )
            _check_no_literal_secrets(value, key_path)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            _check_no_literal_secrets(item, f"{path}[{i}]")


def _apply_env_overrides(data: dict) -> dict:
    """Apply WORKSPACE_<SECTION>__<KEY> environment variable overrides."""
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        rest = env_key[len(ENV_PREFIX):]
        if "__" not in rest:
            continue
        section, _, key = rest.partition("__")
        section = section.lower()
        key = key.lower()
        if section not in data or not isinstance(data.get(section), dict):
            continue
        parsed: Any = env_val
        if env_val.lower() in ("true", "false"):
            parsed = env_val.lower() == "true"
        elif env_val.isdigit():
            parsed = int(env_val)
        data[section][key] = parsed
    return data


def set_disk_uuid(config_path: str | Path, role: str, uuid: str) -> None:
    """Rewrite exactly one line -- `disk.{role}_uuid: "..."` -- in an
    on-disk config.yaml, in place, byte-for-byte identical otherwise
    (comments included). This is the only supported way to flip
    workspace_uuid from blank to a real value when the SSD/NVMe arrives, so
    that step is a single guarded command instead of a hand-edit that risks
    breaking YAML formatting or silently editing the wrong file.

    Deliberately a targeted text substitution, not a yaml.safe_load +
    yaml.safe_dump round-trip -- the latter would silently strip every
    comment from config.yaml, which is exactly the documentation this file
    exists to carry.
    """
    if role not in ("workspace", "backup"):
        raise ConfigError(f"role must be 'workspace' or 'backup', got {role!r}")
    key = f"{role}_uuid"
    path = Path(config_path)
    text = path.read_text()
    pattern = re.compile(rf'^(\s*){key}:\s*(".*?"|\S*)\s*$', re.MULTILINE)
    match = pattern.search(text)
    if not match:
        raise ConfigError(
            f"Could not find a '{key}:' line under disk: in {path} -- "
            "refusing to guess where to add one. Add the key manually first "
            "(see config/config.example.yaml), then re-run this command."
        )
    indent = match.group(1)
    new_line = f'{indent}{key}: "{uuid}"'
    text = text[: match.start()] + new_line + text[match.end():]
    path.write_text(text)


def find_config_file(explicit_path: str | Path | None = None) -> Path:
    if explicit_path:
        p = Path(explicit_path)
        if not p.exists():
            raise ConfigError(f"Config file not found: {p}")
        return p
    env_path = os.environ.get("WORKSPACE_CONFIG_FILE")
    if env_path:
        p = Path(env_path)
        if not p.exists():
            raise ConfigError(f"Config file not found (from WORKSPACE_CONFIG_FILE): {p}")
        return p
    for candidate in DEFAULT_CONFIG_LOCATIONS:
        if candidate.exists():
            return candidate
    raise ConfigError(
        "No config file found. Copy config/config.example.yaml to "
        "config/config.yaml (or /etc/workspace-util/config.yaml on the Pi) "
        "and edit it for this machine."
    )


def load_raw(explicit_path: str | Path | None = None) -> dict:
    path = find_config_file(explicit_path)
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} did not parse to a mapping")
    _check_no_literal_secrets(data)
    data = _apply_env_overrides(data)
    return data


@dataclass
class TestWorkspaceConfig:
    """A small, bounded, SD-card-resident stand-in for the real workspace
    disk -- for exercising Syncthing end-to-end before an SSD/NVMe exists.
    Never used for anything but this: max_bytes keeps it from ever becoming
    a de facto 200GB write onto the boot disk."""
    enabled: bool = False
    path: str = "/var/lib/home-services/compat/test-workspace"
    max_bytes: int = 536_870_912  # 500MB
    __test__ = False  # tell pytest this "Test*"-named dataclass isn't a test case


@dataclass
class WorkspaceConfig:
    local_mac_path: str
    pi_path: str
    projects_subdir: str = "Projects"
    test_workspace: TestWorkspaceConfig = field(default_factory=TestWorkspaceConfig)


@dataclass
class DiskConfig:
    workspace_mount: str = "/mnt/workspace"
    workspace_uuid: str = ""
    backup_mount: str = "/mnt/workspace-backup"
    backup_uuid: str = ""


@dataclass
class SyncConfig:
    enabled: bool = True
    mac_host: str = ""
    pi_host: str = ""
    syncthing_api_port: int = 8384
    ignore_file: str = "config/syncthing-ignore.stignore"


@dataclass
class BackupConfig:
    enabled: bool = True
    destination: str = "/mnt/workspace-backup"
    repository_subdir: str = "restic-repo"
    hourly_keep: int = 24
    daily_keep: int = 14
    weekly_keep: int = 8
    monthly_keep: int = 12
    schedule: str = "hourly"
    integrity_check_schedule: str = "weekly"

    @property
    def repository_path(self) -> str:
        return str(Path(self.destination) / self.repository_subdir)


@dataclass
class DashboardConfig:
    enabled: bool = True
    bind_host: str = ""
    port: int = 8787
    auth_token_env_var: str = "WORKSPACE_DASHBOARD_TOKEN"
    allowed_roots: list[str] = field(default_factory=list)
    # Off by default. When enabled on the Pi, the dashboard exposes one
    # authenticated, confirmed, cancellable power-off action -- never a
    # free-form command or reboot endpoint.
    shutdown_enabled: bool = False
    shutdown_delay_seconds: int = 15
    # Optional stable HTTPS URL (e.g. your Tailscale Funnel/Serve hostname)
    # used only to build a "view this event" link in outgoing notifications.
    # Never required; the link still requires the normal dashboard token.
    external_url: str = ""


@dataclass
class MotionConfig:
    """Tuning for the frame-differencing motion detector used by "Out of the
    House" mode. Deliberately simple (grayscale frame diff on a downscaled
    copy of each sampled frame) rather than an ML model -- this Pi has no GPU
    acceleration worth exploiting for that, and a conservative, explainable
    detector is what a remote-arm/away-from-home feature actually needs."""
    sample_interval_seconds: float = 2.0
    sensitivity: int = 25  # 0-255 per-pixel grayscale difference threshold
    min_changed_area_fraction: float = 0.02  # fraction of the analysis frame that must change
    consecutive_frames: int = 3  # frames in a row over threshold before triggering
    cooldown_seconds: int = 120  # minimum gap between separate *notifications*
    recording_seconds_after_motion: int = 20  # keep recording this long after the last detected motion
    max_recording_seconds: int = 300  # hard cap; continued motion starts a fresh event after this
    recording_fps: int = 8
    analysis_width: int = 160
    analysis_height: int = 120


@dataclass
class CameraConfig:
    """USB camera snapshots + persistent capture served by the dashboard.

    This deliberately describes a single local V4L2 capture device rather
    than a separately exposed streaming server.  The dashboard's existing
    Tailnet/auth boundary therefore also protects every camera image,
    snapshot, and recording -- there is no unauthenticated camera route.
    """
    enabled: bool = False
    device: str = "/dev/video0"
    device_name: str = "Workspace Pi Camera"
    width: int = 640
    height: int = 480
    # The older snapshot/motion worker opens the V4L2 device for individual
    # frames.  Disable it when the dedicated live-stream service owns the
    # device; two independent ffmpeg processes are not reliable with USB UVC
    # cameras.
    background_capture_enabled: bool = True
    refresh_seconds: int = 8  # periodic snapshot interval while idle (not armed)
    # A dedicated, systemd-supervised ffmpeg process creates this HLS stream.
    # It has no listening socket: the dashboard authenticates and serves the
    # playlist and segments through the existing Tailscale Serve HTTPS route.
    live_stream_enabled: bool = False
    live_stream_dir: str = "/run/workspace-camera-hls"
    live_stream_width: int = 640
    live_stream_height: int = 480
    live_stream_fps: int = 10
    live_stream_bitrate_kbps: int = 1200
    live_stream_hls_time_seconds: int = 2
    live_stream_hls_list_size: int = 6
    # Persistent storage. Must be a real, already-mounted, non-root-filesystem
    # location -- see docs/camera.md. Never falls back to writing under the
    # code checkout or the OS disk if this path is unavailable; capture just
    # pauses and the dashboard reports why.
    storage_dir: str = "/mnt/workspace-backup/camera"
    snapshot_retention_days: int = 14
    recording_retention_days: int = 30
    # Capture pauses (and a warning surfaces) once free space on the storage
    # filesystem drops below this. Existing unsynced motion recordings are
    # never deleted just to free space -- see docs/camera.md "Retention".
    min_free_bytes: int = 2_000_000_000
    # Sync persisted media to the Mac via a dedicated, mount-gated Syncthing
    # send-only (Pi) / receive-only (Mac) folder -- see syncthing.py and
    # docs/camera.md. Never deletes the Pi copy after syncing.
    sync_enabled: bool = False
    mac_sync_path: str = ""
    # "Out of the House" armed state persists in logging.dir/camera_state.json
    # across restarts; this is only its value the very first time that file
    # doesn't exist yet.
    out_of_house_default: bool = False
    motion: MotionConfig = field(default_factory=MotionConfig)


@dataclass
class MacBridgeConfig:
    enabled: bool = False
    host: str = ""
    port: int = 8788
    token_env_var: str = "WORKSPACE_MAC_BRIDGE_TOKEN"
    # The Mac bridge can ask the already-running Hammerspoon instance for its
    # explicitly registered dashboard actions.  It is still reached only over
    # the authenticated bridge; this flag provides a simple local opt-out.
    automation_enabled: bool = True
    hammerspoon_cli: str = "hs"


@dataclass
class AutomationHubConfig:
    """Optional Home Platform Control Plane integration.

    The dashboard never accepts a URL from a browser request.  When enabled,
    it talks only to this configured, private Control Plane endpoint, whose
    registry remains the authority for which non-Mac automations exist.
    """
    enabled: bool = False
    control_plane_url: str = ""


@dataclass
class TapoDeviceConfig:
    name: str
    ip: str


@dataclass
class TapoConfig:
    """TP-Link Tapo smart-plug power monitoring, polled in-process the same
    way CameraService owns the USB webcam -- see tapo.py. Devices are
    identified by IP on the local network; the Tapo account is the
    credential used to authenticate to each device, not a cloud API call.
    """
    enabled: bool = False
    username_env_var: str = "WORKSPACE_TAPO_USERNAME"
    password_env_var: str = "WORKSPACE_TAPO_PASSWORD"
    devices: list[TapoDeviceConfig] = field(default_factory=list)
    poll_seconds: int = 10
    retention_days: int = 90
    # Empty means "<logging.dir>/tapo_power.sqlite3" -- see tapo.py.
    database_path: str = ""


@dataclass
class NotificationsConfig:
    enabled: bool = True
    backends: list[dict] = field(default_factory=lambda: [{"type": "log"}])


@dataclass
class LoggingConfig:
    dir: str = "logs"
    level: str = "INFO"


@dataclass
class CommandSpec:
    name: str
    command: list[str]
    allowed_roots: list[str]
    timeout_seconds: int = 60


def resolve_role(configured: str) -> str:
    """Resolve the "mac" | "pi" role this process is running as.

    Explicit config/env value wins (lets a test or an unusual deployment
    force a role). Otherwise inferred from the OS: this utility only ever
    runs on the Mac or on the Pi, and those are always different platforms
    (Darwin vs Linux), so `platform.system()` is a reliable signal here --
    no machine-name allowlist to keep in sync.
    """
    configured = (configured or "auto").strip().lower()
    if configured in ("mac", "pi"):
        return configured
    return "pi" if platform.system() == "Linux" else "mac"


@dataclass
class Config:
    workspace: WorkspaceConfig
    disk: DiskConfig
    sync: SyncConfig
    backup: BackupConfig
    dashboard: DashboardConfig
    mac_bridge: MacBridgeConfig
    notifications: NotificationsConfig
    logging: LoggingConfig
    commands: dict[str, CommandSpec]
    source_path: Path
    role: str = "mac"
    camera: CameraConfig = field(default_factory=CameraConfig)
    automation_hub: AutomationHubConfig = field(default_factory=AutomationHubConfig)
    tapo: TapoConfig = field(default_factory=TapoConfig)

    def resolve_root(self) -> Path:
        """The base directory config-relative paths (like the config file
        itself) are resolved against -- the directory containing config.yaml's
        parent (i.e. the repo/install root)."""
        return self.source_path.parent.parent


def load_config(explicit_path: str | Path | None = None) -> Config:
    path = find_config_file(explicit_path)
    data = load_raw(explicit_path or path)

    def section(name: str) -> dict:
        val = data.get(name, {})
        return val if isinstance(val, dict) else {}

    ws = section("workspace")
    if "local_mac_path" not in ws or "pi_path" not in ws:
        raise ConfigError("config.workspace must define local_mac_path and pi_path")

    commands = {}
    for cmd_name, spec in section("commands").items():
        if not isinstance(spec, dict) or "command" not in spec:
            raise ConfigError(f"config.commands.{cmd_name} must define 'command'")
        commands[cmd_name] = CommandSpec(
            name=cmd_name,
            command=list(spec["command"]),
            allowed_roots=list(spec.get("allowed_roots", [])),
            timeout_seconds=int(spec.get("timeout_seconds", 60)),
        )

    tw = ws.get("test_workspace", {}) if isinstance(ws.get("test_workspace"), dict) else {}

    cam = section("camera")
    motion_raw = cam.get("motion", {}) if isinstance(cam.get("motion"), dict) else {}
    motion_cfg = MotionConfig(**{k: v for k, v in motion_raw.items() if k in MotionConfig.__dataclass_fields__})
    camera_cfg = CameraConfig(
        **{k: v for k, v in cam.items() if k in CameraConfig.__dataclass_fields__ and k != "motion"},
        motion=motion_cfg,
    )

    return Config(
        workspace=WorkspaceConfig(
            local_mac_path=ws["local_mac_path"],
            pi_path=ws["pi_path"],
            projects_subdir=ws.get("projects_subdir", "Projects"),
            test_workspace=TestWorkspaceConfig(
                **{k: v for k, v in tw.items() if k in TestWorkspaceConfig.__dataclass_fields__}
            ),
        ),
        disk=DiskConfig(**{k: v for k, v in section("disk").items() if k in DiskConfig.__dataclass_fields__}),
        sync=SyncConfig(**{k: v for k, v in section("sync").items() if k in SyncConfig.__dataclass_fields__}),
        backup=BackupConfig(**{k: v for k, v in section("backup").items() if k in BackupConfig.__dataclass_fields__}),
        dashboard=DashboardConfig(**{k: v for k, v in section("dashboard").items() if k in DashboardConfig.__dataclass_fields__}),
        camera=camera_cfg,
        mac_bridge=MacBridgeConfig(**{k: v for k, v in section("mac_bridge").items() if k in MacBridgeConfig.__dataclass_fields__}),
        automation_hub=AutomationHubConfig(**{k: v for k, v in section("automation_hub").items() if k in AutomationHubConfig.__dataclass_fields__}),
        tapo=TapoConfig(
            **{k: v for k, v in section("tapo").items() if k in TapoConfig.__dataclass_fields__ and k != "devices"},
            devices=[
                TapoDeviceConfig(name=str(d["name"]), ip=str(d["ip"]))
                for d in section("tapo").get("devices", [])
                if isinstance(d, dict) and "name" in d and "ip" in d
            ],
        ),
        notifications=NotificationsConfig(**{k: v for k, v in section("notifications").items() if k in NotificationsConfig.__dataclass_fields__}),
        logging=LoggingConfig(**{k: v for k, v in section("logging").items() if k in LoggingConfig.__dataclass_fields__}),
        commands=commands,
        source_path=path,
        role=resolve_role(str(data.get("role", os.environ.get("WORKSPACE_ROLE", "auto")))),
    )
