"""Recordings made by the native HLS recorder.

A camera's recordings are listed as two kinds over the same stored chunks:
`motion` (one per motion event, padded by EVENT_PAD seconds) and
`continuous` (one per clock hour that has chunks). Playback is a VOD HLS
playlist over the chunk files; export is an MP4 remux (no re-encode) of the
recording's window. Deletion removes chunk files, never ones that overlap a
protected or still-active recording, and only files this store recorded.
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..db import Database
from ..errors import Conflict, NotFound, ServiceError
from ..runtime import Clock, IdFactory, iso
from ..volumes import VolumeGuard, contained

EVENT_PAD = 5.0
EXPORTS_DIR = "_exports"
EXPORT_TTL_SECONDS = 24 * 3600
_HOUR_ID = re.compile(r"^hr_(\d{10})$")
_CHUNK_NAME = re.compile(r"^c(\d+)\.ts$")


def hour_id(ts: float) -> str:
    return "hr_" + datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d%H")


def hour_start(rid: str) -> float:
    m = _HOUR_ID.match(rid)
    if not m:
        raise NotFound("recording not found")
    return datetime.strptime(m.group(1), "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp()


class CameraStore:
    def __init__(self, config: Config, db: Database, guard: VolumeGuard, clock: Clock, ids: IdFactory):
        self.config, self.db, self.guard, self.clock, self.ids = config, db, guard, clock, ids

    # --- recordings -------------------------------------------------------

    def _window(self, camera_id: str, rid: str) -> dict:
        """started/ended (None while active), kind, protected for one recording."""
        if rid.startswith("evt_"):
            row = self.db.one("SELECT * FROM camera_events WHERE id=? AND camera_id=? AND deleted_at IS NULL", (rid, camera_id))
            if row is None:
                raise NotFound("recording not found", details={"recording_id": rid})
            return {"id": rid, "kind": "motion", "start": row["started_at"], "end": row["ended_at"],
                    "protected": bool(row["protected"]), "pad": EVENT_PAD}
        start = hour_start(rid)
        row = self.db.one("SELECT MIN(start_ts) AS s, MAX(start_ts + duration) AS e FROM camera_chunks"
                          " WHERE camera_id=? AND start_ts >= ? AND start_ts < ?", (camera_id, start, start + 3600))
        if row is None or row["s"] is None:
            raise NotFound("recording not found", details={"recording_id": rid})
        active = self.clock.now() < start + 3600 and self._recorder_alive(camera_id)
        return {"id": rid, "kind": "continuous", "start": row["s"], "end": None if active else row["e"], "protected": False, "pad": 0.0}

    def _recorder_alive(self, camera_id: str) -> bool:
        row = self.db.one("SELECT recorder_heartbeat_at FROM camera_status WHERE camera_id=?", (camera_id,))
        return bool(row and row["recorder_heartbeat_at"] and self.clock.now() - row["recorder_heartbeat_at"] < 30)

    def _chunks(self, camera_id: str, start: float, end: float | None) -> list:
        end = end if end is not None else self.clock.now() + 3600
        return self.db.all("SELECT * FROM camera_chunks WHERE camera_id=? AND start_ts < ? AND start_ts + duration > ? ORDER BY start_ts",
                           (camera_id, end, start))

    def _view(self, camera_id: str, w: dict) -> dict:
        chunks = self._chunks(camera_id, w["start"] - w["pad"], None if w["end"] is None else w["end"] + w["pad"])
        return {"id": w["id"], "camera_id": camera_id, "kind": w["kind"], "started_at": iso(w["start"]),
                "ended_at": iso(w["end"]), "size_bytes": sum(c["bytes"] for c in chunks), "protected": w["protected"],
                "active": w["end"] is None, "playable": bool(chunks)}

    def recording(self, camera_id: str, rid: str) -> dict:
        return self._view(camera_id, self._window(camera_id, rid))

    def recordings(self, camera_id: str, *, limit: int = 50, cursor: str | None = None, kind: str | None = None) -> dict:
        limit = max(1, min(limit, 200))
        before = float(cursor) if cursor else float("inf")
        items = []
        if kind in (None, "motion"):
            for r in self.db.all("SELECT id FROM camera_events WHERE camera_id=? AND deleted_at IS NULL AND started_at < ?"
                                 " ORDER BY started_at DESC LIMIT ?", (camera_id, before, limit + 1)):
                items.append(self._window(camera_id, r["id"]))
        if kind in (None, "continuous"):
            for r in self.db.all("SELECT CAST(start_ts / 3600 AS INTEGER) AS h FROM camera_chunks WHERE camera_id=?"
                                 " GROUP BY h ORDER BY h DESC LIMIT ?", (camera_id, limit + 24)):
                w = self._window(camera_id, hour_id(r["h"] * 3600))
                if w["start"] < before:
                    items.append(w)
        items.sort(key=lambda w: w["start"], reverse=True)
        page = items[:limit]
        next_cursor = repr(page[-1]["start"]) if len(items) > limit and page else None
        return {"recordings": [self._view(camera_id, w) for w in page], "next_cursor": next_cursor}

    def set_protected(self, camera_id: str, rid: str, protected: bool) -> dict:
        w = self._window(camera_id, rid)
        if w["kind"] != "motion":
            raise ServiceError("only motion recordings can be protected", code="not_protectable", status=409)
        self.db.execute("UPDATE camera_events SET protected=? WHERE id=?", (int(protected), rid))
        return self.recording(camera_id, rid)

    # --- deletion ---------------------------------------------------------

    def _kept_windows(self, camera_id: str) -> list[tuple[float, float]]:
        """Windows whose chunks must survive any deletion: protected or active motion recordings."""
        now = self.clock.now()
        return [(r["started_at"] - EVENT_PAD, (r["ended_at"] or now + 3600) + EVENT_PAD)
                for r in self.db.all("SELECT started_at, ended_at FROM camera_events WHERE camera_id=? AND deleted_at IS NULL"
                                     " AND (protected=1 OR ended_at IS NULL)", (camera_id,))]

    def delete_chunks(self, camera_id: str, rows: list, *, keep: list[tuple[float, float]]) -> tuple[int, int]:
        """Delete chunk files and rows unless they overlap a kept window. Returns (chunks, bytes)."""
        root = self.guard.root_path("recordings", write=True)
        n = size = 0
        for c in rows:
            s, e = c["start_ts"], c["start_ts"] + c["duration"]
            if any(s < ke and e > ks for ks, ke in keep):
                continue
            path = contained(root, c["path"])
            path.unlink(missing_ok=True)
            self.db.execute("DELETE FROM camera_chunks WHERE id=?", (c["id"],))
            n, size = n + 1, size + c["bytes"]
        return n, size

    def mark_orphaned_events(self, camera_id: str, reason: str) -> int:
        """Ended, unprotected events with no chunk left are gone."""
        n = 0
        for r in self.db.all("SELECT id, started_at, ended_at FROM camera_events WHERE camera_id=? AND deleted_at IS NULL"
                             " AND ended_at IS NOT NULL AND protected=0", (camera_id,)):
            if not self._chunks(camera_id, r["started_at"] - EVENT_PAD, r["ended_at"] + EVENT_PAD):
                self.db.execute("UPDATE camera_events SET deleted_at=?, delete_reason=? WHERE id=?", (self.clock.now(), reason, r["id"]))
                n += 1
        return n

    def delete_recording(self, camera_id: str, rid: str) -> dict:
        w = self._window(camera_id, rid)
        if w["end"] is None:
            raise Conflict("the recording is still in progress", code="recording_active")
        if w["protected"]:
            raise Conflict("the recording is protected; unprotect it first", code="recording_protected")
        keep = [k for k in self._kept_windows(camera_id)]
        chunks = self._chunks(camera_id, w["start"] - w["pad"], w["end"] + w["pad"])
        n, size = self.delete_chunks(camera_id, chunks, keep=keep)
        if w["kind"] == "motion":
            self.db.execute("UPDATE camera_events SET deleted_at=?, delete_reason='deleted' WHERE id=?", (self.clock.now(), rid))
        self.mark_orphaned_events(camera_id, "deleted")
        return {"deleted_chunks": n, "deleted_bytes": size, "kept_chunks": len(chunks) - n}

    # --- playback and export ----------------------------------------------

    def playlist(self, camera_id: str, rid: str) -> str:
        w = self._window(camera_id, rid)
        chunks = self._chunks(camera_id, w["start"] - w["pad"], None if w["end"] is None else w["end"] + w["pad"])
        if not chunks:
            raise NotFound("no recorded video remains for this recording", code="recording_empty")
        target = int(max(c["duration"] for c in chunks)) + 1
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-MEDIA-SEQUENCE:0",
                 "#EXT-X-PLAYLIST-TYPE:" + ("EVENT" if w["end"] is None else "VOD")]
        prev_end = None
        for c in chunks:
            if c["discontinuity"] or (prev_end is not None and c["start_ts"] - prev_end > 1.5):
                lines.append("#EXT-X-DISCONTINUITY")
            lines += [f"#EXTINF:{c['duration']:.3f},", f"c{c['id']}.ts"]
            prev_end = c["start_ts"] + c["duration"]
        if w["end"] is not None:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def chunk_file(self, camera_id: str, rid: str, name: str) -> Path:
        m = _CHUNK_NAME.match(name)
        if not m:
            raise NotFound("segment not found")
        w = self._window(camera_id, rid)
        row = self.db.one("SELECT * FROM camera_chunks WHERE id=? AND camera_id=?", (int(m.group(1)), camera_id))
        end = (w["end"] if w["end"] is not None else self.clock.now()) + w["pad"]
        if row is None or not (row["start_ts"] < end and row["start_ts"] + row["duration"] > w["start"] - w["pad"]):
            raise NotFound("segment not found")
        path = contained(self.guard.root_path("recordings", write=False), row["path"])
        if not path.is_file():
            raise NotFound("segment not found")
        return path

    def export(self, camera_id: str, rid: str) -> dict:
        """Remux the recording's window into one MP4 under recordings/_exports."""
        w = self._window(camera_id, rid)
        if w["end"] is None:
            raise Conflict("the recording is still in progress", code="recording_active")
        start, end = w["start"] - w["pad"], w["end"] + w["pad"]
        chunks = self._chunks(camera_id, start, end)
        if not chunks:
            raise NotFound("no recorded video remains for this recording", code="recording_empty")
        root = self.guard.root_path("recordings", write=True)
        out_dir = root / EXPORTS_DIR
        out_dir.mkdir(exist_ok=True)
        name = f"{camera_id}-{datetime.fromtimestamp(w['start'], tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{self.ids.new('exp')[4:]}.mp4"
        out = out_dir / name
        listing = out_dir / f".{name}.txt"
        listing.write_text("".join(f"file '{contained(root, c['path'])}'\n" for c in chunks))
        offset = max(0.0, start - chunks[0]["start_ts"])
        try:
            proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                                   "-ss", f"{offset:.3f}", "-t", f"{end - start:.3f}", "-c", "copy", "-movflags", "+faststart",
                                   "-y", str(out)], capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            raise ServiceError("ffmpeg is not installed on this host", code="export_unavailable", status=503) from None
        finally:
            listing.unlink(missing_ok=True)
        self.guard.recheck("recordings", root)
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            raise ServiceError("the export could not be written", code="export_failed", status=500,
                               details={"stderr": proc.stderr[-500:]})
        return {"name": name, "size_bytes": out.stat().st_size, "content_type": "video/mp4"}

    def export_file(self, name: str) -> Path:
        if not re.match(r"^[A-Za-z0-9_.-]+\.mp4$", name):
            raise NotFound("export not found")
        path = contained(self.guard.root_path("recordings", write=False), f"{EXPORTS_DIR}/{name}")
        if not path.is_file():
            raise NotFound("export not found")
        return path

    def purge_exports(self) -> int:
        root = self.guard.root_path("recordings", write=True)
        d = root / EXPORTS_DIR
        n = 0
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and not p.is_symlink() and time.time() - p.stat().st_mtime > EXPORT_TTL_SECONDS:
                    p.unlink()
                    n += 1
        return n


def recordings_usage(db: Database, camera_id: str | None = None) -> int:
    if camera_id:
        row = db.one("SELECT COALESCE(SUM(bytes), 0) AS b FROM camera_chunks WHERE camera_id=?", (camera_id,))
    else:
        row = db.one("SELECT COALESCE(SUM(bytes), 0) AS b FROM camera_chunks")
    return int(row["b"])

