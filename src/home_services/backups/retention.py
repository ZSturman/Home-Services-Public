"""Read-only preview of a restic retention policy.

Reimplements `restic forget`'s keep rules (restic 0.18
`internal/restic/snapshot_policy.go`, default `--group-by host,paths`) over
a snapshot listing, so Home Services can show what a policy would remove
without running `forget` or taking any lock. Snapshot times keep their
recorded UTC offset, as restic's buckets do. tests/test_retention.py checks
the result against `restic forget --dry-run` on generated repositories.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime

_FRACTION = re.compile(r"(\.\d{6})\d+")  # restic writes nanoseconds; Python 3.11 parses microseconds


@dataclass(frozen=True)
class RetentionPolicy:
    last: int = 0
    hourly: int = 0
    daily: int = 0
    weekly: int = 0
    monthly: int = 0
    yearly: int = 0

    def empty(self) -> bool:
        return not any(asdict(self).values())

    def restic_args(self) -> list[str]:
        """The equivalent `restic forget` flags, for the record only."""
        return [a for k, v in asdict(self).items() if v for a in (f"--keep-{k}", str(v))]


def _time(value: str) -> datetime:
    return datetime.fromisoformat(_FRACTION.sub(r"\1", value).replace("Z", "+00:00"))


_BUCKETS = (
    ("last", lambda d, nr: nr, "last snapshot"),
    ("hourly", lambda d, nr: d.year * 1000000 + d.month * 10000 + d.day * 100 + d.hour, "hourly snapshot"),
    ("daily", lambda d, nr: d.year * 10000 + d.month * 100 + d.day, "daily snapshot"),
    ("weekly", lambda d, nr: d.isocalendar()[0] * 100 + d.isocalendar()[1], "weekly snapshot"),
    ("monthly", lambda d, nr: d.year * 100 + d.month, "monthly snapshot"),
    ("yearly", lambda d, nr: d.year, "yearly snapshot"),
)


def apply_policy(snapshots: list[dict], policy: RetentionPolicy) -> tuple[list[dict], list[dict]]:
    """Split one group into (keep, remove), newest first. Each kept item
    gains `reasons`."""
    ordered = sorted(snapshots, key=lambda s: _time(s["time"]), reverse=True)
    counts = {name: getattr(policy, name) for name, _, _ in _BUCKETS}
    last: dict[str, int] = {name: -1 for name, _, _ in _BUCKETS}
    keep, remove = [], []
    for nr, snap in enumerate(ordered):
        t = _time(snap["time"])
        reasons = []
        for name, bucker, reason in _BUCKETS:
            if counts[name] > 0 or counts[name] == -1:
                val = bucker(t, nr)
                # restic also keeps the oldest snapshot while a bucket has counts left
                if val != last[name] or nr == len(ordered) - 1:
                    reasons.append(f"oldest {reason}" if val == last[name] else reason)
                    last[name] = val
                    if counts[name] > 0:
                        counts[name] -= 1
        if reasons:
            keep.append({**snap, "reasons": reasons})
        else:
            remove.append(snap)
    return keep, remove


def preview(snapshots: list[dict], policy: RetentionPolicy) -> dict:
    """What `restic forget` with this policy would keep and remove, per
    host+paths group. Never touches the repository."""
    if policy.empty():
        raise ValueError("no keep rule given; restic would refuse to forget anything")
    groups: dict[tuple, list[dict]] = {}
    for s in snapshots:
        groups.setdefault((s.get("hostname") or "", tuple(sorted(s.get("paths") or []))), []).append(s)
    out = []
    for (host, paths), items in sorted(groups.items()):
        keep, remove = apply_policy(items, policy)
        out.append({
            "host": host, "paths": list(paths),
            "keep": [{"id": s["id"], "time": s["time"], "reasons": s["reasons"]} for s in keep],
            "remove": [{"id": s["id"], "time": s["time"]} for s in remove],
        })
    return {
        "policy": asdict(policy),
        # a display string, never executed
        "restic_equivalent": " ".join(("restic forget", *policy.restic_args(), "--group-by host,paths")),
        "groups": out,
        "totals": {"snapshots": len(snapshots), "keep": sum(len(g["keep"]) for g in out),
                   "remove": sum(len(g["remove"]) for g in out)},
        "note": "preview only: Home Services ran no forget or prune and took no repository lock",
    }
