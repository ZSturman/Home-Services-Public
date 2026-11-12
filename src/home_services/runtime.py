"""Clock and identifier sources, injectable so contract fixtures are
deterministic and tests can move time."""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone


class Clock:
    def now(self) -> float:
        return time.time()


class FixedClock(Clock):
    def __init__(self, start: float):
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class IdFactory:
    def new(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(8)}"

    def secret(self, nbytes: int = 32) -> str:
        return secrets.token_urlsafe(nbytes)


class SequentialIds(IdFactory):
    """Deterministic IDs for fixtures/tests. Never used in production."""

    def __init__(self):
        self.counts: dict[str, int] = {}

    def new(self, prefix: str) -> str:
        n = self.counts.get(prefix, 0) + 1
        self.counts[prefix] = n
        return f"{prefix}_{n:016x}"

    def secret(self, nbytes: int = 32) -> str:
        n = self.counts.get("__secret", 0) + 1
        self.counts["__secret"] = n
        return f"fixture-secret-{n:04d}"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
