"""Pluggable notifications.

No home-lab notification bus was found to integrate with during environment
inspection (the existing "Home Platform" stack on the Pi is MQTT-based but
has no generic alert topic yet), so this ships with a "log" backend that
always runs, plus an optional "webhook" backend (e.g. ntfy.sh, a Slack
incoming webhook, or later a Home Platform MQTT topic) enabled via config.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .logs import get_logger


@dataclass
class Notification:
    level: str  # "info" | "warning" | "critical"
    subject: str
    body: str
    timestamp: float


class NotificationBackend(Protocol):
    def send(self, notification: Notification) -> None: ...


class LogBackend:
    def __init__(self, log_dir: str | Path):
        self.logger = get_logger("health", log_dir)

    def send(self, notification: Notification) -> None:
        log_fn = {"info": self.logger.info, "warning": self.logger.warning}.get(
            notification.level, self.logger.error
        )
        log_fn(f"[{notification.subject}] {notification.body}")


class WebhookBackend:
    def __init__(self, url_env_var: str):
        import os
        self.url = os.environ.get(url_env_var)

    def send(self, notification: Notification) -> None:
        if not self.url:
            return
        import httpx
        try:
            httpx.post(self.url, json={
                "level": notification.level,
                "subject": notification.subject,
                "body": notification.body,
            }, timeout=10)
        except Exception:
            pass  # notification delivery must never crash the caller


class NtfyBackend:
    """Publishes to an ntfy.sh (or self-hosted ntfy) topic.

    Chosen as the default remote channel because it needs no account, no API
    key, and no app-specific payload shape: a phone subscribes to a topic in
    the free ntfy app and immediately receives pushes. `url_env_var` must
    point at the *full* topic URL (e.g. https://ntfy.sh/your-private-topic),
    not just a host, since the topic name is itself unguessable-secret-shaped
    and must never live in this repo.
    """

    def __init__(self, url_env_var: str):
        import os
        self.url = os.environ.get(url_env_var)

    def send(self, notification: Notification) -> None:
        if not self.url:
            return
        import httpx
        priority = {"info": "3", "warning": "4", "critical": "5"}.get(notification.level, "3")
        try:
            httpx.post(
                self.url,
                content=notification.body.encode("utf-8"),
                headers={
                    "Title": notification.subject.encode("utf-8"),
                    "Priority": priority,
                    "Tags": "warning" if notification.level != "info" else "information_source",
                },
                timeout=10,
            )
        except Exception:
            pass  # notification delivery must never crash the caller


class Notifier:
    def __init__(self, backends: list[NotificationBackend]):
        self.backends = backends

    def notify(self, level: str, subject: str, body: str) -> None:
        n = Notification(level=level, subject=subject, body=body, timestamp=time.time())
        for backend in self.backends:
            backend.send(n)


def build_notifier(notifications_cfg, log_dir: str | Path) -> Notifier:
    backends: list[NotificationBackend] = []
    for spec in notifications_cfg.backends:
        btype = spec.get("type")
        if btype == "log":
            backends.append(LogBackend(log_dir))
        elif btype == "webhook":
            backends.append(WebhookBackend(spec.get("url_env_var", "WORKSPACE_WEBHOOK_URL")))
        elif btype == "ntfy":
            backends.append(NtfyBackend(spec.get("url_env_var", "WORKSPACE_NTFY_URL")))
    if not backends:
        backends.append(LogBackend(log_dir))
    return Notifier(backends)


# Standard conditions this system knows how to raise, per docs/architecture.md
# Phase 19. Callers should use these subjects consistently so dashboard/log
# consumers can filter reliably.
SUBJECTS = {
    "sync_failure": "Synchronization failure",
    "mac_unavailable": "Mac unavailable",
    "workspace_disk_missing": "Workspace SSD missing",
    "backup_disk_missing": "Backup HDD missing",
    "backup_failure": "Backup failure",
    "backup_stale": "Backup stale",
    "backup_verify_failure": "Backup verification failure",
    "disk_nearly_full": "Disk nearing capacity",
    "motion_detected": "Motion detected",
    "camera_unavailable": "Camera unavailable",
    "camera_storage_low": "Camera storage nearly full",
}
