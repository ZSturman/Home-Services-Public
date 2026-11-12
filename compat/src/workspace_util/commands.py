"""Whitelisted command runner.

Design constraint (non-negotiable): there is no generic "run this shell
string" endpoint anywhere in this codebase. Every runnable command is a
pre-defined argv list from config.commands, executed with shell=False, in a
directory that must be inside that command's allowed_roots. The web/CLI layer
can only select *which* whitelisted command to run and *which* allowed
subdirectory to run it in -- never what to run.
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import CommandSpec
from .paths import PathNotAllowedError, resolve_within_roots


class CommandNotAllowedError(PermissionError):
    pass


@dataclass
class CommandResult:
    run_id: str
    command_name: str
    argv: list[str]
    cwd: str
    started_at: float
    finished_at: float | None
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return round(self.finished_at - self.started_at, 3)


MAX_OUTPUT_BYTES = 200_000


def run_whitelisted_command(spec: CommandSpec, cwd: str, extra_args: list[str] | None = None) -> CommandResult:
    """Run a single whitelisted command. `cwd` must resolve inside
    spec.allowed_roots -- this is the only user-influenceable input besides
    which whitelisted command name was picked."""
    try:
        resolved_cwd = resolve_within_roots(cwd, spec.allowed_roots)
    except PathNotAllowedError as exc:
        raise CommandNotAllowedError(str(exc)) from exc

    if not resolved_cwd.is_dir():
        raise CommandNotAllowedError(f"{resolved_cwd} is not a directory")

    argv = list(spec.command)
    # extra_args are appended, never interpolated into a shell string, and
    # only allowed for commands that explicitly opt in via config in future;
    # for now they're accepted but unused by any built-in command to keep the
    # attack surface at zero until a real need justifies it.
    run_id = uuid.uuid4().hex[:12]
    started = time.time()
    result = CommandResult(
        run_id=run_id, command_name=spec.name, argv=argv, cwd=str(resolved_cwd),
        started_at=started, finished_at=None, exit_code=None, stdout="", stderr="",
    )
    try:
        proc = subprocess.run(
            argv,
            cwd=str(resolved_cwd),
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
            shell=False,
        )
        result.exit_code = proc.returncode
        result.stdout = proc.stdout[:MAX_OUTPUT_BYTES]
        result.stderr = proc.stderr[:MAX_OUTPUT_BYTES]
    except subprocess.TimeoutExpired as exc:
        result.timed_out = True
        result.error = f"timed out after {spec.timeout_seconds}s"
        result.stdout = (exc.stdout or "")[:MAX_OUTPUT_BYTES] if isinstance(exc.stdout, str) else ""
        result.stderr = (exc.stderr or "")[:MAX_OUTPUT_BYTES] if isinstance(exc.stderr, str) else ""
    except FileNotFoundError as exc:
        result.error = f"command not found: {exc}"
    finally:
        result.finished_at = time.time()

    return result


class CommandHistory:
    """Append-only JSONL execution history, one line per run. Kept simple and
    dependency-free -- no database needed for this volume of records."""

    def __init__(self, history_path: Path):
        self.history_path = history_path
        self.history_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, result: CommandResult) -> None:
        with open(self.history_path, "a") as f:
            f.write(json.dumps(asdict(result)) + "\n")

    def recent(self, limit: int = 50) -> list[dict]:
        if not self.history_path.exists():
            return []
        lines = self.history_path.read_text().splitlines()
        out = []
        for line in reversed(lines[-limit * 2:]):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                break
        return out
