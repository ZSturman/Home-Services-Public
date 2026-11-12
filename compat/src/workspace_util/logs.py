"""Structured, separated logging.

Each subsystem (sync, backup, dashboard, commands, health) writes to its own
log file so `workspace logs <subsystem>` can tail exactly one concern. Log
lines are JSON so they're easy to grep/parse, and a redaction pass strips
anything that looks like a secret before it's ever written to disk.
"""
from __future__ import annotations

import json
import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

SUBSYSTEMS = ("sync", "backup", "dashboard", "commands", "health", "camera", "motion", "tapo")
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

_SECRET_PATTERN = re.compile(
    r"(token|secret|password|api[_-]?key|auth[_-]?key)\s*[=:]\s*\S+", re.I
)


def _redact(message: str) -> str:
    return _SECRET_PATTERN.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + "=<redacted>", message)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "subsystem": getattr(record, "subsystem", "general"),
            "message": _redact(record.getMessage()),
        }
        return json.dumps(payload)


def get_logger(subsystem: str, log_dir: str | Path) -> logging.Logger:
    if subsystem not in SUBSYSTEMS:
        raise ValueError(f"unknown subsystem {subsystem!r}, expected one of {SUBSYSTEMS}")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"workspace_util.{subsystem}")
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        log_dir / f"{subsystem}.log", maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
    )
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False

    old_makeRecord = logger.makeRecord

    def make_record(*args, **kwargs):
        record = old_makeRecord(*args, **kwargs)
        record.subsystem = subsystem
        return record

    logger.makeRecord = make_record  # type: ignore[assignment]
    return logger


def tail_log(subsystem: str, log_dir: str | Path, lines: int = 50) -> list[str]:
    path = Path(log_dir) / f"{subsystem}.log"
    if not path.exists():
        return []
    return path.read_text().splitlines()[-lines:]
