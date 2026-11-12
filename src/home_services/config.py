"""Configuration loading.

Configuration is a TOML file. Secrets are never literal values in it: keys
that look like credentials must end in `_file` (a private host file) or
`_env_var` (an environment variable name). Paths in a development config
are resolved relative to the config file so fixtures stay inside the
checkout's ignored `.dev/` directory.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .backups.retention import RetentionPolicy

ROLES = ("files", "staging", "recordings", "restic_repository", "legacy_media", "companion_recovery")
# Roles the service may write into. Repository data and legacy media are
# read-only to this service by construction.
WRITABLE_ROLES = ("files", "staging", "recordings")

_SECRET_KEY = re.compile(r"(token|secret|password|api_key|auth_key|ntfy_url)$", re.I)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class NetworkConfig:
    port: int = 8790
    bind_addresses: tuple[str, ...] = ("127.0.0.1",)
    bind_interfaces: tuple[str, ...] = ()
    tls_cert_file: str = ""
    tls_key_file: str = ""
    # Address waiting/rebinding: how long to wait for configured interfaces
    # at boot, and how often to re-check that the bound addresses still exist.
    bind_wait_seconds: int = 120
    rebind_check_seconds: int = 15


@dataclass(frozen=True)
class VolumeConfig:
    id: str
    mount: str
    uuid: str
    roots: dict[str, str]
    # Optional expected filesystem type (e.g. "exfat", "ext4").
    fstype: str = ""


@dataclass(frozen=True)
class LimitsConfig:
    volume_reserve_bytes: int = 10_000_000_000
    camera_quota_bytes: int = 200_000_000_000
    staging_cap_bytes: int = 20_000_000_000
    trash_retention_days: int = 7
    staging_abandon_hours: int = 24
    max_chunk_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True)
class RetiredSeries:
    """A repository series nothing writes any more (history kept). It stays
    listed, browsable, and restorable, but raises no overdue/empty alerts."""
    host: str
    tag: str
    reason: str = ""


@dataclass(frozen=True)
class BackupConfig:
    enabled: bool = False
    restic_binary: str = "restic"
    password_file: str = ""
    password_env_var: str = ""
    # Restores land in a fresh directory below this files-root folder.
    restores_dir: str = "Restores"
    lease_seconds: int = 900
    # Non-secret restic repository locator the Files & Backups companion uses
    # for its own restic process (for example an sftp: URL over Tailscale SSH).
    companion_repository: str = ""
    # Optional recovery-only access to the companion's separately owned repository.
    companion_password_file: str = ""
    # A source whose newest repository snapshot is older than this is overdue.
    overdue_hours: float = 26
    # Keep rules for `backups retention-preview` only; nothing applies them.
    retention: RetentionPolicy = RetentionPolicy()
    retired_series: tuple[RetiredSeries, ...] = ()


@dataclass(frozen=True)
class CameraDevice:
    id: str
    name: str
    kind: str = "usb"
    # For the hls-recorder engine: a local live HLS playlist written by the
    # camera's existing producer (read-only; Home Services never opens the device).
    source: str = ""


@dataclass(frozen=True)
class CameraConfig:
    # "none" until the Frigate/go2rtc feasibility gate passes on this host.
    engine: str = "none"
    devices: tuple[CameraDevice, ...] = ()
    event_quiet_seconds: int = 20
    event_max_seconds: int = 300
    notification_cooldown_seconds: int = 120
    # Minutes with motion are kept retention_days, other minutes
    # continuous_retention_hours. Protected/active events are exempt;
    # camera_quota_bytes caps unprotected footage and may expire it sooner.
    retention_days: int = 30
    continuous_retention_hours: int = 168
    # Motion: fraction of 160x90 grey pixels changing by more than the
    # threshold between frames sampled at analysis_fps. Whole-frame changes
    # above max_fraction (exposure jumps, lights switching) are ignored.
    motion_pixel_threshold: int = 25
    motion_min_fraction: float = 0.005
    motion_max_fraction: float = 0.6
    analysis_fps: float = 2
    motion_warmup_seconds: float = 10


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool = False
    ntfy_url_file: str = ""
    ntfy_url_env_var: str = ""
    # Backoff doubles from 15 s to an hourly cap, so 20 attempts span about 13 hours of outage.
    max_attempts: int = 20


@dataclass(frozen=True)
class LegacyContractConfig:
    enabled: bool = True
    # Volume IDs; empty means "not configured" (reported, never guessed).
    workspace_volume: str = ""
    backup_volume: str = ""
    workspace_subdir: str = "Workspace"


@dataclass(frozen=True)
class Config:
    path: Path
    environment: str
    data_dir: Path
    network: NetworkConfig
    mount_probe: str
    mount_fixture_table: Path | None
    volumes: tuple[VolumeConfig, ...]
    limits: LimitsConfig
    backup: BackupConfig
    cameras: CameraConfig
    notifications: NotificationConfig
    legacy_contract: LegacyContractConfig
    media_session_seconds: int = 900

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    def volume(self, volume_id: str) -> VolumeConfig:
        for v in self.volumes:
            if v.id == volume_id:
                return v
        raise ConfigError(f"unknown volume {volume_id!r}")

    def volume_for_role(self, role: str) -> VolumeConfig | None:
        for v in self.volumes:
            if role in v.roots:
                return v
        return None


def _check_no_literal_secrets(data: object, where: str = "") -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            loc = f"{where}.{key}" if where else key
            if isinstance(value, str) and value and _SECRET_KEY.search(key):
                raise ConfigError(
                    f"{loc} looks like a literal secret. Use {key}_file (private host file) "
                    f"or {key}_env_var instead; secrets never live in config files."
                )
            _check_no_literal_secrets(value, loc)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            _check_no_literal_secrets(item, f"{where}[{i}]")


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path).resolve()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from None
    _check_no_literal_secrets(raw)
    base = path.parent

    environment = raw.get("environment", "production")
    if environment not in ("development", "production"):
        raise ConfigError("environment must be 'development' or 'production'")

    net = raw.get("network", {})
    network = NetworkConfig(
        port=int(net.get("port", 8790)),
        bind_addresses=tuple(net.get("bind_addresses", ["127.0.0.1"])),
        bind_interfaces=tuple(net.get("bind_interfaces", [])),
        tls_cert_file=net.get("tls_cert_file", ""),
        tls_key_file=net.get("tls_key_file", ""),
        bind_wait_seconds=int(net.get("bind_wait_seconds", 120)),
        rebind_check_seconds=int(net.get("rebind_check_seconds", 15)),
    )
    if not network.bind_addresses and not network.bind_interfaces:
        raise ConfigError("network.bind_addresses or network.bind_interfaces is required")

    mounts = raw.get("mounts", {})
    probe = mounts.get("probe", "linux")
    if probe not in ("linux", "fixture"):
        raise ConfigError("mounts.probe must be 'linux' or 'fixture'")
    if probe == "fixture" and environment != "development":
        raise ConfigError("the fixture mount probe is only allowed with environment = 'development'")
    fixture_table = _resolve(base, mounts["fixture_table"]) if mounts.get("fixture_table") else None

    volumes = []
    seen_roles: dict[str, str] = {}
    for item in raw.get("volumes", []):
        roots = dict(item.get("roots", {}))
        for role, rel in roots.items():
            if role not in ROLES:
                raise ConfigError(f"volume {item.get('id')!r}: unknown root role {role!r}")
            if Path(rel).is_absolute() or ".." in Path(rel).parts:
                raise ConfigError(f"volume {item.get('id')!r}: root {role} must be a relative path inside the mount")
            if role in seen_roles:
                raise ConfigError(f"root role {role!r} is assigned to both {seen_roles[role]!r} and {item.get('id')!r}")
            seen_roles[role] = item["id"]
        mount = item["mount"]
        if probe == "fixture":
            mount = str(_resolve(base, mount))
        if not item.get("uuid"):
            raise ConfigError(f"volume {item.get('id')!r}: uuid is required; directory existence is not proof of a disk")
        volumes.append(VolumeConfig(id=item["id"], mount=mount, uuid=item["uuid"], roots=roots, fstype=item.get("fstype", "")))
    # Distinct roles must not overlap one another on the same volume.
    for v in volumes:
        parts = {role: Path(rel).parts for role, rel in v.roots.items()}
        for a, pa in parts.items():
            for b, pb in parts.items():
                if a < b and (pa[: len(pb)] == pb or pb[: len(pa)] == pa):
                    raise ConfigError(f"volume {v.id!r}: roots {a} and {b} overlap")
    if "staging" in seen_roles and "files" in seen_roles and seen_roles["staging"] != seen_roles["files"]:
        raise ConfigError("staging and files roots must be on the same volume for atomic publication")

    lim = raw.get("limits", {})
    limits = LimitsConfig(**{k: int(v) for k, v in lim.items()})

    b = raw.get("backup", {})
    backup = BackupConfig(
        enabled=bool(b.get("enabled", False)),
        restic_binary=b.get("restic_binary", "restic"),
        password_file=str(_resolve(base, b["password_file"])) if b.get("password_file") else "",
        password_env_var=b.get("password_env_var", ""),
        restores_dir=b.get("restores_dir", "Restores"),
        lease_seconds=int(b.get("lease_seconds", 900)),
        companion_repository=b.get("companion_repository", ""),
        companion_password_file=str(_resolve(base, b["companion_password_file"])) if b.get("companion_password_file") else "",
        overdue_hours=float(b.get("overdue_hours", 26)),
        retention=RetentionPolicy(**{k.removeprefix("keep_"): int(v) for k, v in b.get("retention", {}).items()}),
        retired_series=tuple(RetiredSeries(**r) for r in b.get("retired_series", [])),
    )
    if backup.enabled and "restic_repository" not in seen_roles:
        raise ConfigError("backup.enabled requires a volume with a restic_repository root")
    if ("companion_recovery" in seen_roles) != bool(backup.companion_password_file):
        raise ConfigError("companion_recovery root and backup.companion_password_file must be configured together")

    c = raw.get("cameras", {})
    cameras = CameraConfig(
        engine=c.get("engine", "none"),
        devices=tuple(CameraDevice(**d) for d in c.get("devices", [])),
        event_quiet_seconds=int(c.get("event_quiet_seconds", 20)),
        event_max_seconds=int(c.get("event_max_seconds", 300)),
        notification_cooldown_seconds=int(c.get("notification_cooldown_seconds", 120)),
        retention_days=int(c.get("retention_days", 30)),
        continuous_retention_hours=int(c.get("continuous_retention_hours", 168)),
        motion_pixel_threshold=int(c.get("motion_pixel_threshold", 25)),
        motion_min_fraction=float(c.get("motion_min_fraction", 0.005)),
        motion_max_fraction=float(c.get("motion_max_fraction", 0.6)),
        analysis_fps=float(c.get("analysis_fps", 2)),
        motion_warmup_seconds=float(c.get("motion_warmup_seconds", 10)),
    )
    if cameras.engine not in ("none", "hls-recorder"):
        raise ConfigError(f"cameras.engine {cameras.engine!r} is not supported; use 'none' or 'hls-recorder' (see docs/CAMERA_GATE.md)")
    if cameras.engine == "hls-recorder":
        for d in cameras.devices:
            if not d.source.endswith(".m3u8") or not Path(d.source).is_absolute():
                raise ConfigError(f"camera {d.id!r}: the hls-recorder engine needs source = an absolute path to a live .m3u8 playlist")

    n = raw.get("notifications", {})
    notifications = NotificationConfig(
        enabled=bool(n.get("enabled", False)),
        ntfy_url_file=str(_resolve(base, n["ntfy_url_file"])) if n.get("ntfy_url_file") else "",
        ntfy_url_env_var=n.get("ntfy_url_env_var", ""),
        max_attempts=int(n.get("max_attempts", 20)),
    )

    lc = raw.get("legacy_contract", {})
    legacy = LegacyContractConfig(
        enabled=bool(lc.get("enabled", True)),
        workspace_volume=lc.get("workspace_volume", ""),
        backup_volume=lc.get("backup_volume", ""),
        workspace_subdir=lc.get("workspace_subdir", "Workspace"),
    )
    ids = {v.id for v in volumes}
    for vid in (legacy.workspace_volume, legacy.backup_volume):
        if vid and vid not in ids:
            raise ConfigError(f"legacy_contract references unknown volume {vid!r}")

    data_dir = _resolve(base, raw.get("data_dir", "/var/lib/home-services"))
    return Config(
        path=path, environment=environment, data_dir=data_dir, network=network,
        mount_probe=probe, mount_fixture_table=fixture_table, volumes=tuple(volumes),
        limits=limits, backup=backup, cameras=cameras, notifications=notifications,
        legacy_contract=legacy, media_session_seconds=int(raw.get("media_session_seconds", 900)),
    )


def read_secret(file_path: str, env_var: str) -> str | None:
    """Read a secret from a private file (preferred) or an environment
    variable. Returns None when neither is configured/present; callers fail
    closed. The value is never logged."""
    if file_path:
        p = Path(file_path)
        try:
            mode = p.stat().st_mode & 0o077
        except FileNotFoundError:
            return None
        if mode:
            raise ConfigError(f"secret file {file_path} must not be readable by group/other (chmod 600)")
        return p.read_text().strip() or None
    if env_var:
        return os.environ.get(env_var) or None
    return None
