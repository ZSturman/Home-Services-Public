"""TP-Link Tapo smart-plug power monitoring and control.

Ported from the standalone `tapo-power-dashboard` prototype's polling logic,
rewritten async-native (the `tapo` library is asyncio-only) and folded into
the dashboard process the same way CameraService owns the USB webcam: one
background task started from the dashboard's lifespan, one SQLite file for
history, one place every route reads from. Unlike the prototype, this adds
on/off control -- the `tapo` library already supports it; the original
chose read-only, this dashboard's auth boundary makes it safe to add.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, TapoDeviceConfig
from .logs import get_logger

_RETENTION_INTERVAL_SECONDS = 3600


def database_path(cfg: Config) -> Path:
    if cfg.tapo.database_path:
        return Path(cfg.tapo.database_path)
    return Path(cfg.logging.dir) / "tapo_power.sqlite3"


def _connect(cfg: Config) -> sqlite3.Connection:
    path = database_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(cfg: Config) -> None:
    with closing(_connect(cfg)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                ts REAL NOT NULL,
                device_name TEXT NOT NULL,
                ip TEXT NOT NULL,
                watts REAL,
                device_on INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings(device_name, ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_status (
                device_name TEXT PRIMARY KEY,
                ip TEXT NOT NULL,
                last_seen_at REAL,
                last_watts REAL,
                last_device_on INTEGER,
                last_error TEXT
            )
        """)
        conn.commit()


def extract_watts(payload: dict[str, Any]) -> float | None:
    """`tapo`-lib result field names have moved between versions; try the
    ones seen in practice, in both watt and milliwatt form."""
    for key in ("current_power", "current_power_w", "power", "power_w"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    for key in ("current_power_mw", "power_mw"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value) / 1000.0
    return None


def _as_dict(result: Any) -> dict[str, Any]:
    for attr in ("to_dict", "dict"):
        method = getattr(result, attr, None)
        if callable(method):
            try:
                return method()
            except TypeError:
                pass
    if isinstance(result, dict):
        return result
    return {k: v for k, v in vars(result).items() if not k.startswith("_")}


@dataclass
class PowerReading:
    device_name: str
    ip: str
    watts: float | None
    device_on: bool | None
    error: str | None
    last_seen_at: float | None


class TapoService:
    """One instance per dashboard process, created lazily like
    CameraService. `perform` methods are safe to call even when the
    background poll loop isn't running (e.g. a one-off on/off request)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.logger = get_logger("tapo", cfg.logging.dir)
        self._task = None
        self._client = None
        self._latest: dict[str, PowerReading] = {}
        self._last_retention_at = 0.0
        # Status/history routes must work even when polling is disabled
        # (tapo.enabled: false) or hasn't started yet -- e.g. reporting
        # "not configured" rather than crashing on a missing table.
        init_db(cfg)

    @property
    def devices(self) -> list[TapoDeviceConfig]:
        return self.cfg.tapo.devices

    def _credentials(self) -> tuple[str, str]:
        username = os.environ.get(self.cfg.tapo.username_env_var, "")
        password = os.environ.get(self.cfg.tapo.password_env_var, "")
        if not username or not password:
            raise RuntimeError(
                f"{self.cfg.tapo.username_env_var}/{self.cfg.tapo.password_env_var} are not set"
            )
        return username, password

    async def _get_client(self):
        from tapo import ApiClient  # imported lazily: optional dependency, Pi-only

        if self._client is None:
            username, password = self._credentials()
            self._client = ApiClient(username, password)
        return self._client

    async def _connect_device(self, ip: str):
        client = await self._get_client()
        return await client.p110(ip)

    def start(self) -> None:
        import asyncio

        if not self.cfg.tapo.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run_forever(), name="tapo-poll")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run_forever(self) -> None:
        import asyncio

        interval = max(5, int(self.cfg.tapo.poll_seconds))
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a single bad poll must never kill the loop
                self.logger.warning("tapo poll cycle failed: %s", exc)
            self._maybe_run_retention()
            await asyncio.sleep(interval)

    async def poll_once(self) -> list[PowerReading]:
        readings = []
        for device_cfg in self.devices:
            readings.append(await self._poll_device(device_cfg))
        return readings

    async def _poll_device(self, device_cfg: TapoDeviceConfig) -> PowerReading:
        now = time.time()
        try:
            device = await self._connect_device(device_cfg.ip)
            power_result = await device.get_current_power()
            info_result = await device.get_device_info()
            watts = extract_watts(_as_dict(power_result))
            device_on = _as_dict(info_result).get("device_on")
            reading = PowerReading(
                device_name=device_cfg.name, ip=device_cfg.ip, watts=watts,
                device_on=bool(device_on) if device_on is not None else None,
                error=None, last_seen_at=now,
            )
        except Exception as exc:
            previous = self._latest.get(device_cfg.name)
            reading = PowerReading(
                device_name=device_cfg.name, ip=device_cfg.ip,
                watts=previous.watts if previous else None,
                device_on=previous.device_on if previous else None,
                error=str(exc),
                last_seen_at=previous.last_seen_at if previous else None,
            )
        self._latest[device_cfg.name] = reading
        self._record(reading, now)
        return reading

    def _record(self, reading: PowerReading, now: float) -> None:
        with closing(_connect(self.cfg)) as conn:
            if reading.error is None:
                conn.execute(
                    "INSERT INTO readings (ts, device_name, ip, watts, device_on) VALUES (?, ?, ?, ?, ?)",
                    (now, reading.device_name, reading.ip, reading.watts,
                     None if reading.device_on is None else int(reading.device_on)),
                )
            conn.execute(
                """INSERT INTO device_status (device_name, ip, last_seen_at, last_watts, last_device_on, last_error)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(device_name) DO UPDATE SET
                     ip=excluded.ip,
                     last_seen_at=COALESCE(excluded.last_seen_at, device_status.last_seen_at),
                     last_watts=COALESCE(excluded.last_watts, device_status.last_watts),
                     last_device_on=COALESCE(excluded.last_device_on, device_status.last_device_on),
                     last_error=excluded.last_error""",
                (reading.device_name, reading.ip, reading.last_seen_at, reading.watts,
                 None if reading.device_on is None else int(reading.device_on), reading.error),
            )
            conn.commit()

    def _maybe_run_retention(self) -> None:
        now = time.time()
        if now - self._last_retention_at < _RETENTION_INTERVAL_SECONDS:
            return
        self._last_retention_at = now
        cutoff = now - self.cfg.tapo.retention_days * 86400
        with closing(_connect(self.cfg)) as conn:
            conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
            conn.commit()

    def status(self) -> dict:
        """Current status per configured device -- the poll loop's cache if
        it's running, otherwise the last row `device_status` remembers."""
        result = []
        cached = {r.device_name: r for r in self._latest.values()}
        with closing(_connect(self.cfg)) as conn:
            rows = {
                row[0]: row for row in conn.execute(
                    "SELECT device_name, ip, last_seen_at, last_watts, last_device_on, last_error FROM device_status"
                )
            }
        for device_cfg in self.devices:
            live = cached.get(device_cfg.name)
            row = rows.get(device_cfg.name)
            if live is not None:
                result.append({
                    "name": live.device_name, "ip": live.ip, "watts": live.watts,
                    "device_on": live.device_on, "error": live.error, "last_seen_at": live.last_seen_at,
                })
            elif row is not None:
                result.append({
                    "name": row[0], "ip": row[1], "last_seen_at": row[2],
                    "watts": row[3], "device_on": bool(row[4]) if row[4] is not None else None, "error": row[5],
                })
            else:
                result.append({
                    "name": device_cfg.name, "ip": device_cfg.ip, "watts": None,
                    "device_on": None, "error": "no reading yet", "last_seen_at": None,
                })
        return {"enabled": self.cfg.tapo.enabled, "devices": result}

    def history(self, device_name: str, hours: float) -> list[dict]:
        cutoff = time.time() - hours * 3600
        with closing(_connect(self.cfg)) as conn:
            rows = conn.execute(
                "SELECT ts, watts, device_on FROM readings WHERE device_name = ? AND ts >= ? ORDER BY ts",
                (device_name, cutoff),
            ).fetchall()
        return [{"ts": r[0], "watts": r[1], "device_on": bool(r[2]) if r[2] is not None else None} for r in rows]

    def summary(self, device_name: str, hours: float) -> dict:
        points = self.history(device_name, hours)
        watt_values = [p["watts"] for p in points if p["watts"] is not None]
        if not watt_values:
            return {"device_name": device_name, "hours": hours, "samples": 0, "avg_watts": None, "peak_watts": None, "estimated_kwh": None}
        avg_watts = sum(watt_values) / len(watt_values)
        # Crude power x time integration: same approach as the original
        # prototype -- average power over the window times the window
        # duration, not a proper trapezoidal integral over irregular gaps.
        estimated_kwh = (avg_watts * hours) / 1000.0
        return {
            "device_name": device_name, "hours": hours, "samples": len(watt_values),
            "avg_watts": avg_watts, "peak_watts": max(watt_values), "estimated_kwh": estimated_kwh,
        }

    async def set_power(self, device_name: str, on: bool) -> PowerReading:
        device_cfg = next((d for d in self.devices if d.name == device_name), None)
        if device_cfg is None:
            raise KeyError(f"unknown Tapo device: {device_name}")
        device = await self._connect_device(device_cfg.ip)
        if on:
            await device.on()
        else:
            await device.off()
        return await self._poll_device(device_cfg)
