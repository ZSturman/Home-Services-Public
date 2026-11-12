"""Git-aware read-only inspection.

This module only ever reads Git state (status/branch/log). It never commits,
resets, stashes, or otherwise mutates a repository -- Syncthing and Git are
kept as separate concepts, per design principle #4.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GitState:
    path: str
    is_repo: bool
    branch: str | None = None
    clean: bool = True
    modified_count: int = 0
    untracked_count: int = 0
    ahead: int | None = None
    behind: int | None = None
    last_commit_iso: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def _run_git(repo_path: Path, args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def scan_repo(path: Path) -> GitState:
    """Cheap, read-only Git status for a single directory. ahead/behind is
    only computed if it's inexpensive (no network fetch -- uses whatever the
    local remote-tracking refs already know)."""
    if not is_git_repo(path):
        return GitState(path=str(path), is_repo=False)

    state = GitState(path=str(path), is_repo=True)

    try:
        branch_res = _run_git(path, ["branch", "--show-current"])
        state.branch = branch_res.stdout.strip() or None

        status_res = _run_git(path, ["status", "--porcelain=v1"])
        if status_res.returncode != 0:
            state.error = status_res.stderr.strip()
            return state
        lines = [l for l in status_res.stdout.splitlines() if l.strip()]
        state.untracked_count = sum(1 for l in lines if l.startswith("??"))
        state.modified_count = sum(1 for l in lines if not l.startswith("??"))
        state.clean = len(lines) == 0

        rev_res = _run_git(path, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
        if rev_res.returncode == 0:
            parts = rev_res.stdout.strip().split()
            if len(parts) == 2:
                state.ahead, state.behind = int(parts[0]), int(parts[1])

        log_res = _run_git(path, ["log", "-1", "--format=%cI"])
        if log_res.returncode == 0:
            state.last_commit_iso = log_res.stdout.strip() or None

        if state.modified_count > 0 and state.untracked_count > 0:
            state.warnings.append(
                "Uncommitted changes present -- if this repo is also being edited "
                "on another synced machine right now, verify before syncing further."
            )
    except subprocess.TimeoutExpired:
        state.error = "git command timed out"
    except FileNotFoundError:
        state.error = "git is not installed"

    return state
