"""Read-only access to the predecessor's stored snapshots and motion events.

Layout (predecessor docs/camera.md):
  <legacy_media>/events/<event-id>/meta.json, recording.mp4, pre-NN.jpg
  <legacy_media>/snapshots/<YYYY-MM-DD>/<HH-MM-SS-ffffff>.jpg
Nothing here writes, renames, or deletes. A recorded `synced` flag is
reported as the predecessor's claim, not as verified protection.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..errors import NotFound, ServiceError
from ..runtime import iso
from ..volumes import VolumeGuard, contained

def _when(value):
    """The predecessor wrote epoch floats; fixtures and older events may hold ISO strings."""
    return iso(float(value)) if isinstance(value, (int, float)) else value


_EVENT_ID = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{4,16}$")
_MEDIA_NAME = re.compile(r"^(recording\.mp4|pre-[0-9]{2}\.jpg)$")
CONTENT_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg"}


class LegacyMedia:
    def __init__(self, guard: VolumeGuard):
        self.guard = guard

    def _root(self) -> Path:
        return self.guard.root_path("legacy_media", write=False)

    def events(self, limit: int = 50, before: str | None = None) -> list[dict]:
        events_dir = self._root() / "events"
        if not events_dir.is_dir():
            return []
        ids = sorted((p.name for p in events_dir.iterdir() if p.is_dir() and _EVENT_ID.match(p.name)), reverse=True)
        if before:
            ids = [i for i in ids if i < before]
        out = []
        for event_id in ids[: max(1, min(limit, 200))]:
            try:
                out.append(self.event(event_id))
            except ServiceError:
                continue  # unreadable metadata is skipped, never fatal
        return out

    def event(self, event_id: str) -> dict:
        if not _EVENT_ID.match(event_id):
            raise NotFound("event not found")
        d = contained(self._root(), f"events/{event_id}")
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            raise NotFound("event metadata missing or unreadable", details={"event_id": event_id}) from None
        media = sorted(p.name for p in d.iterdir() if _MEDIA_NAME.match(p.name) and p.is_file() and not p.is_symlink())
        return {"id": event_id, "source": "legacy", "read_only": True,
                "started_at": _when(meta.get("start_ts")), "last_motion_at": _when(meta.get("last_motion_ts")),
                "ended_at": _when(meta.get("end_ts")),
                "camera_name": meta.get("camera_device_name"), "media": media,
                "legacy_sync_claim": {"synced": meta.get("synced"), "note": "reported by the predecessor; not verified as a copy"}}

    def media_path(self, resource_id: str) -> Path:
        event_id, _, name = resource_id.partition("/")
        if not _EVENT_ID.match(event_id) or not _MEDIA_NAME.match(name):
            raise NotFound("media not found")
        p = contained(self._root(), f"events/{event_id}/{name}")
        if not p.is_file() or p.is_symlink():
            raise NotFound("media not found")
        return p
