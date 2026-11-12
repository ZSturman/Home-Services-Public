"""Native HLS recorder (`cameras.engine = "hls-recorder"`).

Consumes a live HLS playlist that the camera's existing producer already
writes (on the Pi, the predecessor's FFmpeg stream), so Home Services never
opens the device and never competes for it. For each new MPEG-TS segment:

- its bytes are appended unchanged to the current chunk file under the
  verified recordings root (a chunk rotates every CHUNK_SECONDS, at a
  discontinuity, or when the date changes); nothing is transcoded, and
  nothing is written while the volume is not verified;
- a few downscaled grey frames are decoded and differenced to find motion,
  which the EventGrouper turns into motion events (and, when the camera is
  armed and outside the notification cooldown, one ntfy message per event).

Retention runs every few minutes: minutes without motion are kept
`continuous_retention_hours` (7 days), motion `retention_days` (30 days).
Protected/active events are exempt; `camera_quota_bytes` caps the total,
oldest unprotected footage first, and may expire footage sooner.
Runs as `home-services worker camera`, a separately supervised process.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import CameraDevice
from ..errors import ServiceError
from ..gate.synthetic import ANALYSIS_H, ANALYSIS_W
from ..services import Services
from .events import EventGrouper, MotionEvent
from .store import EVENT_PAD, CameraStore, recordings_usage

log = logging.getLogger("home_services.camera")

CHUNK_SECONDS = 60
STALE_SECONDS = 15
RETENTION_INTERVAL_SECONDS = 300
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+\.ts$")


@dataclass
class Segment:
    seq: int
    name: str
    duration: float
    discontinuity: bool


def parse_playlist(text: str) -> list[Segment]:
    seq, duration, disc = 0, 0.0, False
    out: list[Segment] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line == "#EXT-X-DISCONTINUITY":
            disc = True
        elif line and not line.startswith("#"):
            if _SEGMENT.match(line):
                out.append(Segment(seq, line, duration, disc))
            seq, disc = seq + 1, False
    return out


@dataclass
class MotionDetector:
    threshold: int
    min_fraction: float
    max_fraction: float
    fps: float
    # After the stream (re)starts the camera settles exposure and focus;
    # changes in that window are not motion.
    warmup_seconds: float = 10
    _prev: bytes | None = None
    _run: int = 0
    _seen: int = 0

    def reset(self) -> None:
        self._prev, self._run, self._seen = None, 0, 0

    def frames(self, path: Path) -> list[bytes]:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf",
                               f"fps={self.fps},scale={ANALYSIS_W}:{ANALYSIS_H},format=gray", "-f", "rawvideo", "-"],
                              capture_output=True, timeout=30)
        size = ANALYSIS_W * ANALYSIS_H
        data = proc.stdout
        return [data[i:i + size] for i in range(0, len(data) - size + 1, size)]

    def analyse(self, path: Path) -> list[int]:
        """Indexes of frames that continue a run of at least two changed frames."""
        hits = []
        n = ANALYSIS_W * ANALYSIS_H
        t = self.threshold
        for i, frame in enumerate(self.frames(path)):
            self._seen += 1
            if self._seen <= self.warmup_seconds * self.fps:
                self._prev, self._run = frame, 0
                continue
            if self._prev is not None:
                changed = sum(1 for a, b in zip(self._prev, frame) if a - b > t or b - a > t) / n
                self._run = self._run + 1 if self.min_fraction < changed < self.max_fraction else 0
                if self._run >= 2:  # one noisy frame pair is not motion
                    hits.append(i)
            self._prev = frame
        return hits


@dataclass
class Chunk:
    rel: str
    start: float
    duration: float = 0.0
    bytes: int = 0
    motion_frames: int = 0
    discontinuity: bool = False


@dataclass
class CameraRecorder:
    s: Services
    device: CameraDevice
    store: CameraStore
    grouper: EventGrouper
    detector: MotionDetector
    last_seq: int | None = None
    chunk: Chunk | None = None
    last_segment_at: float | None = None
    state: str = "starting"
    detail: str = ""
    _events: dict[str, MotionEvent] = field(default_factory=dict)

    @property
    def source(self) -> Path:
        return Path(self.device.source)

    # --- chunks -----------------------------------------------------------

    def _close_chunk(self) -> None:
        self.chunk = None  # its row is already current: rows are updated after every segment

    def _save_chunk(self) -> None:
        c = self.chunk
        self.s.db.execute("UPDATE camera_chunks SET duration=?, bytes=?, motion_frames=? WHERE path=?",
                          (c.duration, c.bytes, c.motion_frames, c.rel))

    def _write(self, seg: Segment, data: bytes, start: float, discontinuity: bool) -> None:
        root = self.s.guard.root_path("recordings", write=True)  # raises unless the volume is verified
        day = datetime.fromtimestamp(start, tz=timezone.utc)
        c = self.chunk
        if c is not None and (discontinuity or c.duration >= CHUNK_SECONDS or not c.rel.startswith(f"{self.device.id}/{day:%Y-%m-%d}/")):
            self._close_chunk()
            c = None
        if c is None:
            rel = f"{self.device.id}/{day:%Y-%m-%d}/{day:%H%M%S}-{seg.seq}.ts"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            c = self.chunk = Chunk(rel, start, discontinuity=discontinuity)
            # Registered before the first byte, so a crash never leaves a file retention cannot see.
            self.s.db.execute("INSERT OR IGNORE INTO camera_chunks (camera_id, start_ts, duration, path, bytes, discontinuity)"
                              " VALUES (?,?,0,?,0,?)", (self.device.id, start, rel, int(discontinuity)))
        with open(root / c.rel, "ab") as f:
            f.write(data)
            f.flush()
        self.s.guard.recheck("recordings", root)
        c.duration += seg.duration
        c.bytes += len(data)

    # --- motion -----------------------------------------------------------

    def _armed(self) -> bool:
        row = self.s.db.one("SELECT armed FROM camera_state WHERE camera_id=?", (self.device.id,))
        return bool(row and row["armed"])

    def _motion(self, t: float) -> None:
        ev, closed = self.grouper.motion(self.device.id, t)
        self._persist_closed(closed)
        if ev.id not in self._events:
            self._events[ev.id] = ev
            notify = ev.notify and self._armed()
            self.s.db.execute("INSERT OR IGNORE INTO camera_events (id, camera_id, started_at, last_motion_at, notified) VALUES (?,?,?,?,?)",
                              (ev.id, self.device.id, ev.started_at, ev.last_motion_at, int(notify)))
            log.info("camera %s: motion event %s started", self.device.id, ev.id)
            if notify:
                at = time.strftime("%H:%M", time.localtime(t))
                self.s.outbox.enqueue(f"motion:{ev.id}", f"Motion: {self.device.name}", f"Motion detected at {at}.",
                                      priority="high", tags=["camera"])
        else:
            self.s.db.execute("UPDATE camera_events SET last_motion_at=? WHERE id=?", (t, ev.id))

    def _persist_closed(self, closed: list[MotionEvent]) -> None:
        for ev in closed:
            self._events.pop(ev.id, None)
            self.s.db.execute("UPDATE camera_events SET last_motion_at=?, ended_at=?, end_reason=? WHERE id=?",
                              (ev.last_motion_at, ev.ended_at, ev.end_reason, ev.id))
            log.info("camera %s: motion event %s ended (%s)", self.device.id, ev.id, ev.end_reason)

    # --- main step --------------------------------------------------------

    def step(self) -> None:
        now = self.s.clock.now()
        try:
            segments = parse_playlist(self.source.read_text())
        except OSError:
            segments = []
            self.state, self.detail = "offline", "the camera's live playlist is not available"
        if segments:
            newest = segments[-1].seq
            restart = self.last_seq is not None and newest < self.last_seq
            if restart:
                log.warning("camera %s: producer restarted (sequence %d -> %d)", self.device.id, self.last_seq, newest)
            first_run = self.last_seq is None
            if first_run or restart:
                self.detector.reset()
                self.last_seq = segments[-2].seq if len(segments) > 1 else newest - 1
            fresh = [x for x in segments if x.seq > self.last_seq]
            gap = bool(fresh) and fresh[0].seq != self.last_seq + 1
            for i, seg in enumerate(fresh):
                self._take(seg, discontinuity=(restart or gap or seg.discontinuity or first_run) if i == 0 else seg.discontinuity)
                self.last_seq = seg.seq
            if not fresh and self.last_segment_at is not None and now - self.last_segment_at > STALE_SECONDS:
                self.state, self.detail = "stale", f"no new video for {now - self.last_segment_at:.0f} s"
        self._persist_closed(self.grouper.tick(now, self.device.id))
        self.s.db.execute(
            "INSERT INTO camera_status (camera_id, recorder_heartbeat_at, last_segment_at, state, detail, updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(camera_id) DO UPDATE SET recorder_heartbeat_at=excluded.recorder_heartbeat_at,"
            " last_segment_at=excluded.last_segment_at, state=excluded.state, detail=excluded.detail, updated_at=excluded.updated_at",
            (self.device.id, now, self.last_segment_at, self.state, self.detail, now))

    def _take(self, seg: Segment, *, discontinuity: bool) -> None:
        path = self.source.parent / seg.name
        try:
            data = path.read_bytes()
            start = path.stat().st_mtime - seg.duration
        except OSError:
            return  # rotated away before we read it: the next one carries a gap marker
        if discontinuity:
            self.detector.reset()
        try:
            hits = self.detector.analyse(path)
        except (OSError, subprocess.TimeoutExpired):
            hits = []
        try:
            self._write(seg, data, start, discontinuity)
            self.state, self.detail = "recording", ""
        except ServiceError as exc:
            self._close_chunk_quietly()
            self.state, self.detail = "storage_unavailable", exc.message
        if self.chunk is not None:
            self.chunk.motion_frames += len(hits)
            self._save_chunk()
        for i in hits:
            self._motion(start + i / self.detector.fps)
        self.last_segment_at = self.s.clock.now()

    def _close_chunk_quietly(self) -> None:
        try:
            self._close_chunk()
        except ServiceError:
            self.chunk = None

    def shutdown(self) -> None:
        self._close_chunk_quietly()
        now = self.s.clock.now()
        for ev in list(self._events.values()):
            self.s.db.execute("UPDATE camera_events SET ended_at=?, end_reason='recorder_stopped' WHERE id=? AND ended_at IS NULL",
                              (max(ev.last_motion_at, now), ev.id))

    # --- retention --------------------------------------------------------

    def retention(self) -> dict:
        cfg, cam = self.s.config.cameras, self.device.id
        now = self.s.clock.now()
        keep_all = self.store._kept_windows(cam)
        motion = [(r["started_at"] - EVENT_PAD, (r["ended_at"] or now) + EVENT_PAD)
                  for r in self.s.db.all("SELECT started_at, ended_at FROM camera_events WHERE camera_id=? AND deleted_at IS NULL", (cam,))]
        continuous_cutoff = now - cfg.continuous_retention_hours * 3600
        motion_cutoff = now - cfg.retention_days * 86400
        old = self.s.db.all("SELECT * FROM camera_chunks WHERE camera_id=? AND start_ts + duration < ? ORDER BY start_ts", (cam, continuous_cutoff))
        n1, b1 = self.store.delete_chunks(cam, [c for c in old if c["start_ts"] + c["duration"] < motion_cutoff], keep=keep_all)
        n2, b2 = self.store.delete_chunks(cam, self.s.db.all("SELECT * FROM camera_chunks WHERE camera_id=? AND start_ts + duration < ?",
                                                             (cam, continuous_cutoff)), keep=keep_all + motion)
        n3 = b3 = 0
        quota = self.s.config.limits.camera_quota_bytes
        usage = recordings_usage(self.s.db)
        if usage > quota:
            excess = usage - int(quota * 0.95)
            for c in self.s.db.all("SELECT * FROM camera_chunks WHERE camera_id=? ORDER BY start_ts LIMIT 2000", (cam,)):
                if excess <= 0:
                    break
                n, b = self.store.delete_chunks(cam, [c], keep=keep_all)
                n3, b3, excess = n3 + n, b3 + b, excess - b
        expired = self.store.mark_orphaned_events(cam, "expired")
        exports = self.store.purge_exports()
        result = {"expired_chunks": n1 + n2, "quota_chunks": n3, "freed_bytes": b1 + b2 + b3, "expired_events": expired, "exports_purged": exports}
        if n1 + n2 + n3:
            log.info("camera %s retention: %s", cam, result)
        return result


def run_cameras(s: Services, stop, poll_seconds: float = 1.0) -> None:
    cfg = s.config.cameras
    if cfg.engine != "hls-recorder":
        log.warning("camera worker idle: cameras.engine is %r", cfg.engine)
        stop.event.wait()
        return
    store = CameraStore(s.config, s.db, s.guard, s.clock, s.ids)
    grouper = EventGrouper(cfg.event_quiet_seconds, cfg.event_max_seconds, cfg.notification_cooldown_seconds)
    recorders = [CameraRecorder(s, d, store, grouper, MotionDetector(cfg.motion_pixel_threshold, cfg.motion_min_fraction,
                                                                     cfg.motion_max_fraction, cfg.analysis_fps,
                                                                     cfg.motion_warmup_seconds))
                 for d in cfg.devices]
    s.db.execute("UPDATE camera_events SET ended_at=last_motion_at + ?, end_reason='recorder_restart' WHERE ended_at IS NULL",
                 (cfg.event_quiet_seconds,))
    next_retention = 0.0
    log.info("camera worker recording %s", [d.id for d in cfg.devices])
    try:
        while not stop.requested:
            for r in recorders:
                try:
                    r.step()
                except Exception:  # one camera's failure must not stop the others
                    log.exception("camera %s step failed", r.device.id)
            now = time.monotonic()
            if now >= next_retention:
                for r in recorders:
                    try:
                        r.retention()
                    except ServiceError as exc:
                        log.warning("camera %s retention skipped: %s", r.device.id, exc.message)
                next_retention = now + RETENTION_INTERVAL_SECONDS
            stop.event.wait(poll_seconds)
    finally:
        for r in recorders:
            r.shutdown()
        log.info("camera worker stopped")
