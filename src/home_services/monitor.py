"""Health monitor: turns state changes into outbox notifications.

Runs inside the notifier process. Each check reports a set of currently
active problems keyed by a stable name. A problem is announced once after it
has persisted for its grace period and announced again only when it clears
(and, if it comes back, as a new episode). Finished restore jobs and backup
runs are announced once each. Messages name volumes, hosts, tags, and job
IDs only: never file paths, URLs, tokens, or repository passwords.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass

from .db import loads
from .errors import ServiceError
from .services import Services

log = logging.getLogger("home_services.monitor")

STATE_KEY = "monitor_state"
LOW_STATE_STORE_BYTES = 2_000_000_000
STALE_JOB_SECONDS = 600
# Problems produced only by the (slower) backup checks; carried over unchanged by ticks that skip them.
BACKUP_PROBLEM_PREFIXES = ("backup_overdue:", "backup_empty:", "repository:")


@dataclass(frozen=True)
class Problem:
    key: str
    title: str
    body: str
    priority: str = "high"
    grace_seconds: float = 120
    recovered_title: str = ""
    recovered_body: str = ""


def _age(seconds: float) -> str:
    if seconds >= 2 * 86400:
        return f"{seconds / 86400:.0f} days"
    if seconds >= 2 * 3600:
        return f"{seconds / 3600:.0f} hours"
    return f"{max(1, seconds / 60):.0f} minutes"


class Monitor:
    def __init__(self, s: Services):
        self.s = s
        # Problem keys of retired series: their episodes end without a "recovered" message.
        self.retired_keys: set[str] = set()

    # --- checks -----------------------------------------------------------

    def problems(self, *, include_backups: bool) -> list[Problem]:
        s = self.s
        out: list[Problem] = []
        for v in s.config.volumes:
            st = s.guard.status(v)
            if not st.verified:
                what = {"missing": "is not mounted", "wrong_volume": "has the wrong disk mounted",
                        "wrong_fstype": "has an unexpected filesystem"}.get(st.state, "is unavailable")
                out.append(Problem(f"volume:{v.id}", "Storage disk unavailable",
                                   f"Volume {v.id} {what}. File storage and backups on it are paused; nothing is written elsewhere.",
                                   recovered_title="Storage disk available again",
                                   recovered_body=f"Volume {v.id} is mounted and verified again."))
        try:
            free = shutil.disk_usage(s.config.data_dir).free
        except OSError:
            free = None
        if free is not None and free < LOW_STATE_STORE_BYTES:
            out.append(Problem("state_store:low_space", "Home Services system disk nearly full",
                               f"Only {free / 1e9:.1f} GB is free for Home Services state.", grace_seconds=600,
                               recovered_title="Home Services system disk has space again",
                               recovered_body="The state store has enough free space again."))
        for job in s.jobs.list(state="running", limit=200):
            age = job["heartbeat"]["age_seconds"]
            if age is not None and age > STALE_JOB_SECONDS:
                out.append(Problem(f"job_stale:{job['id']}", "Home Services job not responding",
                                   f"Job {job['id']} ({job['kind']}) has not reported for {_age(age)}.", priority="default",
                                   grace_seconds=0))
        if s.cameras.engine_active:
            for d in s.config.cameras.devices:
                st = s.cameras.status(d.id)
                if st["state"] in ("stale", "offline", "recorder_unavailable", "storage_unavailable"):
                    out.append(Problem(f"camera:{d.id}", "Camera not recording",
                                       f"{d.name}: {st['state'].replace('_', ' ')}. {st.get('reason') or ''}".strip(),
                                       grace_seconds=180, recovered_title="Camera recording again",
                                       recovered_body=f"{d.name} is recording again."))
        if include_backups and s.config.backup.enabled:
            out += self._backup_problems()
        return out

    def _backup_problems(self) -> list[Problem]:
        s = self.s
        status = s.restic.repository_status()
        if status["state"] != "present" or not status["password_configured"]:
            # Volume loss is reported by the volume check; this covers a missing repository or password.
            if status["state"] == "unavailable":
                return []
            return [Problem("repository:unavailable", "Backup repository unavailable",
                            "The backup repository is missing or its password is not configured.", grace_seconds=300,
                            recovered_title="Backup repository available again", recovered_body="The backup repository can be read again.")]
        try:
            groups = s.backups.groups()
        except ServiceError as exc:
            return [Problem("repository:unreadable", "Backup repository unreadable",
                            f"Listing snapshots failed ({exc.code}). Backups may be failing to authenticate or the repository may be damaged.",
                            grace_seconds=600, recovered_title="Backup repository readable again",
                            recovered_body="Snapshots can be listed again.")]
        out = []
        for r in s.db.all("SELECT id FROM backup_sources WHERE repository != 'primary'"):
            src = s.backups.source(r["id"])
            if src["protection"]["state"] == "overdue":
                label = f"{src['host']} ({src['tag']})"
                out.append(Problem(f"backup_overdue:{src['id']}", "Backup overdue",
                                   f"{label} last reported a successful backup {_age(src['protection']['age_seconds'])} ago.",
                                   priority="default", grace_seconds=0, recovered_title="Backup current again",
                                   recovered_body=f"{label} reported a successful backup again."))
        watched = {r["id"] for r in s.db.all("SELECT id FROM backup_sources")}
        for g in groups:
            p = g["protection"]
            if g.get("retired"):  # history only: nothing is expected to write it
                tags = ','.join(sorted(g['tags']))
                self.retired_keys |= {f"backup_empty:{g['hostname']}:{tags}",
                                      f"backup_overdue:{g['source_id'] or g['hostname'] + ':' + tags}"}
                continue
            if p.get("empty"):
                label = f"{g['hostname']} ({', '.join(g['tags']) or 'untagged'})"
                out.append(Problem(f"backup_empty:{g['hostname']}:{','.join(sorted(g['tags']))}", "Backup contains no files",
                                   f"The newest {label} snapshot is empty: the backup runs but protects nothing.",
                                   priority="default", grace_seconds=0, recovered_title="Backup contains files again",
                                   recovered_body=f"The newest {label} snapshot has files again."))
            if p["state"] != "overdue":
                continue
            # Watch registered sources, and any series whose host has no current series (e.g. a Mac that stopped backing up).
            host_current = any(o["hostname"] == g["hostname"] and o["protection"]["state"] == "current"
                               for o in groups if not o.get("retired"))
            if g["source_id"] not in watched and host_current:
                continue
            label = f"{g['hostname']} ({', '.join(g['tags']) or 'untagged'})"
            key = f"backup_overdue:{g['source_id'] or g['hostname'] + ':' + ','.join(sorted(g['tags']))}"
            out.append(Problem(key, "Backup overdue",
                               f"{label} was last backed up {_age(p['age_seconds'])} ago.", priority="default", grace_seconds=0,
                               recovered_title="Backup current again", recovered_body=f"{label} has a recent snapshot again."))
        return out

    def finished_work(self, since: float) -> list[tuple[str, str, str, str]]:
        """(dedupe_key, title, body, priority) for restores and backup runs
        that reached a terminal state after `since`."""
        s = self.s
        out = []
        for row in s.db.all("SELECT * FROM jobs WHERE kind='backup.restore' AND finished_at > ? ORDER BY finished_at", (since,)):
            state, result = row["state"], loads(row["result"]) or {}
            summary = result.get("summary") or {}
            if state == "succeeded":
                files = summary.get("files_restored", summary.get("total_files"))
                size = summary.get("bytes_restored", summary.get("total_bytes"))
                detail = f"{files} files" if files is not None else "done"
                if size is not None:
                    detail += f", {size / 1e6:.1f} MB"
                out.append((f"job:{row['id']}", "Restore finished", f"Restore {row['id']}: {detail}. Files are under Restores/{row['id']}.", "low"))
            elif state in ("failed", "interrupted"):
                code = (loads(row["error"]) or {}).get("code", state)
                out.append((f"job:{row['id']}", "Restore did not finish", f"Restore {row['id']} {state} ({code}).", "default"))
        for row in s.db.all("SELECT r.*, s.host, s.tag FROM backup_runs r JOIN backup_sources s ON s.id = r.source_id"
                            " WHERE r.finished_at > ? AND r.state IN ('failed','interrupted') ORDER BY r.finished_at", (since,)):
            code = (loads(row["error"]) or {}).get("code", row["state"])
            out.append((f"run:{row['id']}", "Backup did not finish", f"{row['host']} ({row['tag']}) backup {row['state']} ({code}).", "high"))
        return out

    # --- state machine ----------------------------------------------------

    def _load(self) -> dict:
        raw = self.s.db.get_meta(STATE_KEY)
        return json.loads(raw) if raw else {}

    def _save(self, state: dict) -> None:
        self.s.db.set_meta(STATE_KEY, json.dumps(state, sort_keys=True))

    def tick(self, *, include_backups: bool = True) -> dict:
        """Evaluate once and enqueue any notifications. Returns counts."""
        s = self.s
        now = s.clock.now()
        state = self._load()
        active: dict = state.get("active", {})
        since = state.get("finished_since")
        if since is None:
            since = now  # first run: do not announce history
        current = {p.key: p for p in self.problems(include_backups=include_backups)}
        if not include_backups:  # keep backup episodes untouched between the slower backup checks
            current.update({k: None for k in active if k.startswith(BACKUP_PROBLEM_PREFIXES)})
        sent = 0
        for key, p in current.items():
            if p is None:
                continue
            entry = active.setdefault(key, {"since": now, "announced": False, "recovered_title": p.recovered_title,
                                            "recovered_body": p.recovered_body})
            if not entry["announced"] and now - entry["since"] >= p.grace_seconds:
                _, inserted = s.outbox.enqueue(f"{key}:{entry['since']:.0f}", p.title, p.body, priority=p.priority)
                entry["announced"] = True
                sent += int(inserted)
        for key in [k for k in active if k not in current]:
            entry = active.pop(key)
            if entry["announced"] and entry.get("recovered_title") and key not in self.retired_keys:
                _, inserted = s.outbox.enqueue(f"{key}:{entry['since']:.0f}:recovered", entry["recovered_title"],
                                               entry["recovered_body"], priority="default")
                sent += int(inserted)
        for dedupe, title, body, priority in self.finished_work(since):
            _, inserted = s.outbox.enqueue(dedupe, title, body, priority=priority)
            sent += int(inserted)
        self._save({"active": active, "finished_since": now})
        return {"enqueued": sent, "active_problems": sorted(active)}
