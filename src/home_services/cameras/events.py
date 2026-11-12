"""Motion-event grouping and notification cooldown.

Engine-independent: an engine adapter feeds raw motion signals per camera;
this turns them into events (one open event per camera, extended while motion
continues, closed after the quiet window or at the hard cap) and decides
whether an event may notify. Recording and notification are independent:
cooldown suppresses notifications, never recording.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MotionEvent:
    id: str
    camera_id: str
    started_at: float
    last_motion_at: float
    ended_at: float | None = None
    end_reason: str | None = None
    notify: bool = False

    @property
    def open(self) -> bool:
        return self.ended_at is None


@dataclass
class EventGrouper:
    quiet_seconds: float = 20
    max_seconds: float = 300
    cooldown_seconds: float = 120
    _open: dict[str, MotionEvent] = field(default_factory=dict)
    _last_notified: dict[str, float] = field(default_factory=dict)
    _seq: int = 0

    def _new_id(self, camera_id: str, t: float) -> str:
        self._seq += 1
        return f"evt_{camera_id}_{int(t)}_{self._seq}"

    def motion(self, camera_id: str, t: float) -> tuple[MotionEvent, list[MotionEvent]]:
        """Record a motion signal at time t. Returns (current event, events
        closed by this call)."""
        closed = self.tick(t, camera_id)
        ev = self._open.get(camera_id)
        if ev is not None:
            ev.last_motion_at = t
            return ev, closed
        last = self._last_notified.get(camera_id)
        notify = last is None or t - last >= self.cooldown_seconds
        ev = MotionEvent(self._new_id(camera_id, t), camera_id, t, t, notify=notify)
        if notify:
            self._last_notified[camera_id] = t
        self._open[camera_id] = ev
        return ev, closed

    def tick(self, t: float, camera_id: str | None = None) -> list[MotionEvent]:
        """Close events that went quiet or hit the cap."""
        closed = []
        for cid in [camera_id] if camera_id else list(self._open):
            ev = self._open.get(cid)
            if ev is None:
                continue
            if t - ev.started_at >= self.max_seconds:
                ev.ended_at, ev.end_reason = ev.started_at + self.max_seconds, "max_duration"
            elif t - ev.last_motion_at >= self.quiet_seconds:
                ev.ended_at, ev.end_reason = ev.last_motion_at + self.quiet_seconds, "quiet"
            if ev.ended_at is not None:
                closed.append(self._open.pop(cid))
        return closed
