"""Supervised worker processes.

`home-services worker storage`, `home-services worker backup`, and
`home-services notifier` each run as their own systemd unit, so a failing
backup restore cannot take file storage or notification delivery down with
it, and none of them depends on the gateway process staying up.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time

import httpx

from .errors import ServiceError
from .services import Services

log = logging.getLogger("home_services.worker")
LEASE_SECONDS = 120
# Heartbeats and volume re-checks while a job runs. Short, so a vanished disk
# stops a restore within seconds instead of after the job has "finished".
BEAT_SECONDS = 5
CLEANUP_INTERVAL_SECONDS = 3600


class Stop:
    def __init__(self):
        self.event = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: self.event.set())
            except ValueError:
                pass  # not the main thread (tests)

    @property
    def requested(self) -> bool:
        return self.event.is_set()


class JobContext:
    """Renews a job lease from a background thread and tracks why the job
    should stop: cancellation, lost lease, volume loss, or worker shutdown."""

    def __init__(self, s: Services, job: dict, owner: str, stop: Stop, guard_check=None, interval: float = BEAT_SECONDS):
        self.s, self.job, self.owner, self.stop = s, job, owner, stop
        self.guard_check = guard_check
        self.interval = interval
        self.progress: dict | None = None
        self.reason: str | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._done.set()
        self._thread.join(timeout=5)
        return False

    def beat(self) -> None:
        try:
            if self.s.jobs.heartbeat(self.job["id"], self.owner, LEASE_SECONDS, self.progress):
                self.reason = self.reason or "cancelled"
        except ServiceError:
            self.reason = "lease_lost"
        if self.guard_check is not None and self.reason is None:
            try:
                self.guard_check()
            except ServiceError:
                self.reason = "volume_unavailable"
        if self.stop.requested and self.reason is None:
            self.reason = "worker_stopping"

    def _loop(self):
        while not self._done.wait(self.interval):
            self.beat()

    def should_stop(self) -> bool:
        return self.reason is not None


def _owner(queue: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{queue}"


def run_storage_cleanup(s: Services, job: dict, owner: str, stop: Stop) -> None:
    try:
        result = {"uploads": s.transfers.cleanup_abandoned(), "trash_purged": s.storage.purge_expired_trash()}
    except ServiceError as exc:
        s.jobs.finish(job["id"], owner, "blocked", error={"code": exc.code, "message": exc.message, **exc.details})
        return
    s.jobs.finish(job["id"], owner, "succeeded", result=result)


def run_restore(s: Services, job: dict, owner: str, stop: Stop) -> None:
    params = job["params"]
    try:
        files_root = s.storage.files_root(write=True)
        restores = files_root / s.config.backup.restores_dir
        restores.mkdir(exist_ok=True)
        target = restores / job["id"]
        if target.exists():
            s.jobs.finish(job["id"], owner, "failed", error={"code": "destination_exists",
                          "message": "restore destinations are never reused", "path": str(target.relative_to(files_root))})
            return
        target.mkdir()
    except ServiceError as exc:
        s.jobs.finish(job["id"], owner, "blocked", error={"code": exc.code, "message": exc.message, **exc.details})
        return
    rel = str(target.relative_to(files_root))
    with JobContext(s, job, owner, stop, guard_check=lambda: s.guard.recheck("files", files_root)) as ctx:
        def on_progress(p: dict):
            ctx.progress = p
        try:
            summary = s.recovery.restore(params["snapshot_id"], params["include"], target, on_progress=on_progress, should_stop=ctx.should_stop)
        except ServiceError as exc:
            ctx.reason = ctx.reason or "failed"
            summary = {"error": {"code": exc.code, "message": exc.message, **exc.details}}
        ctx.beat()  # final lease renewal and volume re-check: success is only reported for data on the verified disk
        if ctx.reason is None and not summary.get("stopped"):
            total, restored = summary.get("total_bytes"), summary.get("bytes_restored")
            if total is not None and restored is not None and restored < total:
                ctx.reason = "failed"
                summary = {**summary, "error": {"code": "restore_incomplete",
                                                "message": f"restic restored {restored} of {total} bytes"}}
    reason = ctx.reason
    if reason == "lease_lost":
        log.warning("job %s lease lost; leaving state to the reaper", job["id"])
        return
    result = {"destination": {"root": "files", "path": rel}, "summary": summary}
    if reason is None and not summary.get("stopped"):
        s.jobs.finish(job["id"], owner, "succeeded", result=result)
    elif reason == "cancelled":
        s.jobs.finish(job["id"], owner, "cancelled", result=result)
    elif reason == "worker_stopping":
        s.jobs.finish(job["id"], owner, "interrupted", result=result,
                      error={"code": "worker_stopped", "message": "the worker stopped; partial data is left in the destination"})
    elif reason == "volume_unavailable":
        s.jobs.finish(job["id"], owner, "failed", result=result,
                      error={"code": "volume_unavailable", "message": "the files volume was lost during the restore"})
    else:
        s.jobs.finish(job["id"], owner, "failed", result=result, error=summary.get("error"))


HANDLERS = {"storage.cleanup": run_storage_cleanup, "backup.restore": run_restore}


def work_once(s: Services, queue: str, owner: str, stop: Stop) -> bool:
    """Reap, claim, and run at most one job. Returns True if a job ran."""
    s.jobs.reap_expired()
    job = s.jobs.claim(queue, owner, LEASE_SECONDS)
    if job is None:
        return False
    log.info("running job %s (%s)", job["id"], job["kind"])
    try:
        HANDLERS[job["kind"]](s, job, owner, stop)
    except Exception as exc:  # a handler bug must not kill the worker
        log.exception("job %s crashed", job["id"])
        try:
            s.jobs.finish(job["id"], owner, "failed", error={"code": "internal_error", "message": type(exc).__name__})
        except ServiceError:
            pass
    return True


def schedule_maintenance(s: Services) -> None:
    bucket = int(s.clock.now() // CLEANUP_INTERVAL_SECONDS)
    s.jobs.create("storage.cleanup", {}, idempotency_key=f"scheduled:{bucket}", created_by="scheduler")


def run_worker(s: Services, queue: str, poll_seconds: float = 2.0) -> None:
    stop = Stop()
    owner = _owner(queue)
    log.info("worker %s started", owner)
    while not stop.requested:
        if queue == "storage":
            schedule_maintenance(s)
        if not work_once(s, queue, owner, stop):
            stop.event.wait(poll_seconds)
    log.info("worker %s stopped", owner)


MONITOR_SECONDS = 60
BACKUP_MONITOR_SECONDS = 600


def run_notifier(s: Services, poll_seconds: float = 5.0) -> None:
    """Evaluate the health monitor and deliver the outbox. Monitoring keeps
    running while ntfy is unreachable or unconfigured; messages wait in the
    outbox and retry with backoff."""
    from .monitor import Monitor

    stop = Stop()
    monitor = Monitor(s)
    warned = False
    next_check = next_backup_check = 0.0
    with httpx.Client() as client:
        while not stop.requested:
            now = time.monotonic()
            if now >= next_check:
                include_backups = now >= next_backup_check
                try:
                    result = monitor.tick(include_backups=include_backups)
                    if result["enqueued"]:
                        log.info("monitor: %s", result)
                except Exception:  # a monitor bug must not stop delivery
                    log.exception("monitor tick failed")
                next_check = now + MONITOR_SECONDS
                if include_backups:
                    next_backup_check = now + BACKUP_MONITOR_SECONDS
            try:
                url = s.outbox.ntfy_url()
            except ServiceError as exc:
                if not warned:
                    log.warning("notifier not delivering: %s", exc.message)
                    warned = True
                stop.event.wait(30)
                continue
            warned = False
            result = s.outbox.deliver_due(client, url)
            if result["retrying_or_failed"]:
                log.warning("notification delivery: %s", result)
            stop.event.wait(poll_seconds)
