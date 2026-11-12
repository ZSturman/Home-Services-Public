"""Camera registry, status, and capability reporting.

With `cameras.engine = "none"` every recording/live/motion capability is
unavailable with the reason `engine_not_configured`. With the native
`hls-recorder` engine, each capability is reported from what is true right
now: the recorder's heartbeat, whether the camera's playlist is fresh, and
whether the recordings volume is verified. Cameras default to disarmed;
arming only enables motion notifications (recording does not depend on it).
"""
from __future__ import annotations

import os

from ..config import Config
from ..db import Database
from ..errors import CapabilityUnavailable, NotFound, ServiceError
from ..runtime import Clock, iso
from ..volumes import VolumeGuard

CAMERA_CAPABILITIES = ("live_view", "snapshot", "recording", "motion_events", "history", "playback", "export", "deletion",
                       "notifications", "arming")
RECORDER_STALE_SECONDS = 30
FEED_STALE_SECONDS = 15
NOT_CONFIGURED = {"state": "unavailable", "reason": "engine_not_configured",
                  "message": "No recording engine is configured on this host (cameras.engine = \"none\")."}


class CameraRegistry:
    def __init__(self, config: Config, db: Database, guard: VolumeGuard, clock: Clock):
        self.config, self.db, self.guard, self.clock = config, db, guard, clock

    def device(self, camera_id: str):
        dev = next((d for d in self.config.cameras.devices if d.id == camera_id), None)
        if dev is None:
            raise NotFound("camera not found", details={"camera_id": camera_id})
        return dev

    def _armed(self, camera_id: str) -> tuple[bool, float | None]:
        row = self.db.one("SELECT armed, updated_at FROM camera_state WHERE camera_id=?", (camera_id,))
        return (bool(row["armed"]), row["updated_at"]) if row else (False, None)

    @property
    def engine_active(self) -> bool:
        return self.config.cameras.engine == "hls-recorder"

    def status(self, camera_id: str) -> dict:
        """state: recording | stale | offline | recorder_unavailable | storage_unavailable | not_managed"""
        dev = self.device(camera_id)
        now = self.clock.now()
        if not self.engine_active:
            return {"state": "not_managed", "reason": "engine_not_configured", "observed_at": iso(now), "recording": False}
        row = self.db.one("SELECT * FROM camera_status WHERE camera_id=?", (camera_id,))
        try:
            feed_age = now - os.stat(dev.source).st_mtime
        except OSError:
            feed_age = None
        base = {"observed_at": iso(now), "last_video_at": iso(row["last_segment_at"]) if row else None}
        if row is None or row["recorder_heartbeat_at"] is None or now - row["recorder_heartbeat_at"] > RECORDER_STALE_SECONDS:
            live = feed_age is not None and feed_age < FEED_STALE_SECONDS
            return {**base, "state": "recorder_unavailable", "reason": "the camera recorder process is not running",
                    "recording": False, "live_available": live}
        if row["state"] == "storage_unavailable":
            return {**base, "state": "storage_unavailable", "reason": row["detail"], "recording": False}
        if feed_age is None:
            return {**base, "state": "offline", "reason": "the camera's live stream is not available", "recording": False}
        if feed_age > FEED_STALE_SECONDS or row["state"] == "stale":
            return {**base, "state": "stale", "reason": f"no new video for {max(feed_age, 0):.0f} s", "recording": False}
        return {**base, "state": "recording", "reason": None, "recording": True}

    def capabilities(self, camera_id: str | None = None) -> dict:
        if not self.engine_active or camera_id is None:
            return {name: dict(NOT_CONFIGURED) for name in CAMERA_CAPABILITIES}
        st = self.status(camera_id)
        try:
            self.guard.root_path("recordings", write=False)
            storage = None
        except ServiceError as exc:
            storage = exc.message

        def cap(ok: bool, reason: str = "", message: str = "", **extra) -> dict:
            return {"state": "available", **extra} if ok else {"state": "unavailable", "reason": reason, "message": message, **extra}

        live_ok = st["state"] in ("recording",) or st.get("live_available", False)
        rec_ok = st["state"] == "recording"
        history_ok = storage is None
        notify_ok = self.config.notifications.enabled
        return {
            "live_view": cap(live_ok, st["state"], st.get("reason") or "", protocol="hls"),
            "snapshot": cap(live_ok, st["state"], st.get("reason") or "", format="jpeg", max_width=1280),
            "recording": cap(rec_ok, st["state"], st.get("reason") or "", mode="continuous+motion",
                             continuous_retention_hours=self.config.cameras.continuous_retention_hours,
                             motion_retention_days=self.config.cameras.retention_days),
            "motion_events": cap(rec_ok, st["state"], st.get("reason") or "", detector="frame_difference"),
            "history": cap(history_ok, "storage_unavailable", storage or ""),
            "playback": cap(history_ok, "storage_unavailable", storage or "", protocol="hls"),
            "export": cap(history_ok, "storage_unavailable", storage or "", format="mp4"),
            "deletion": cap(history_ok, "storage_unavailable", storage or "", granularity="recording"),
            "notifications": cap(notify_ok, "notifications_disabled", "ntfy delivery is not enabled on this host", transport="ntfy"),
            "arming": cap(True, effect="motion notifications only; recording continues while disarmed"),
        }

    def list(self) -> list[dict]:
        return [self.get(d.id) for d in self.config.cameras.devices]

    def get(self, camera_id: str) -> dict:
        dev = self.device(camera_id)
        armed, at = self._armed(camera_id)
        st = self.status(camera_id)
        st["armed"] = armed
        return {"id": dev.id, "name": dev.name, "kind": dev.kind, "engine": self.config.cameras.engine,
                "armed": armed, "armed_updated_at": iso(at), "status": st, "capabilities": self.capabilities(camera_id)}

    def set_armed(self, camera_id: str, armed: bool) -> dict:
        self.device(camera_id)
        if armed and not self.engine_active:
            raise CapabilityUnavailable("cannot arm: no recording engine is configured on this host",
                                        details={"capability": "arming", **NOT_CONFIGURED})
        self.db.execute("INSERT INTO camera_state (camera_id, armed, updated_at) VALUES (?,?,?)"
                        " ON CONFLICT(camera_id) DO UPDATE SET armed=excluded.armed, updated_at=excluded.updated_at",
                        (camera_id, int(armed), self.clock.now()))
        return self.get(camera_id)

    def snapshot(self, camera_id: str, width: int = 640) -> bytes:
        """A JPEG of the newest frame in the camera's live stream (the first,
        independent frame of the newest complete segment)."""
        import subprocess
        from pathlib import Path

        from .recorder import parse_playlist
        dev = self.live_source(camera_id)
        try:
            segments = parse_playlist(Path(dev.source).read_text())
        except OSError:
            segments = []
        if not segments:
            raise ServiceError("the camera's live stream is not available", code="camera_offline", status=503)
        seg = Path(dev.source).parent / segments[-1].name
        try:
            proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(seg), "-frames:v", "1",
                                   "-vf", f"scale={max(64, min(width, 1280))}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "-"],
                                  capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is None or proc.returncode != 0 or not proc.stdout:
            raise ServiceError("could not decode a frame from the live stream", code="snapshot_failed", status=503)
        return proc.stdout

    def live_source(self, camera_id: str):
        """The directory and playlist name of a camera's live HLS output."""
        dev = self.device(camera_id)
        if not self.engine_active:
            raise CapabilityUnavailable("live view through Home Services is not configured on this host",
                                        details={"capability": "live_view", **NOT_CONFIGURED})
        caps = self.capabilities(camera_id)["live_view"]
        if caps["state"] != "available":
            raise CapabilityUnavailable("the camera's live stream is not available right now",
                                        details={"capability": "live_view", "reason": caps.get("reason"), "message": caps.get("message")})
        return dev
