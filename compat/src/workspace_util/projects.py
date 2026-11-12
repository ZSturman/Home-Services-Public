"""Project discovery -- walks the workspace's Projects directory and reports
Git-aware state for each top-level project. Read-only; see git_scan.py.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .git_scan import GitState, scan_repo


@dataclass
class ProjectSummary:
    name: str
    path: str
    git: GitState
    size_bytes: int | None
    last_modified: float | None


def _dir_size(path: Path, max_entries: int = 200_000) -> int | None:
    """Best-effort recursive size, skipping heavy ignored dirs for speed.
    Returns None if it can't be computed within max_entries (avoids hanging
    on huge trees when called from a request handler)."""
    total = 0
    count = 0
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".build", "DerivedData"}
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            for entry in current.iterdir():
                count += 1
                if count > max_entries:
                    return None
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name in skip_dirs:
                        continue
                    stack.append(entry)
                elif entry.is_file():
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        pass
        except (PermissionError, FileNotFoundError):
            continue
    return total


def discover_projects(projects_root: Path, compute_size: bool = False) -> list[ProjectSummary]:
    if not projects_root.is_dir():
        return []
    summaries = []
    for entry in sorted(projects_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        git = scan_repo(entry)
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = None
        size = _dir_size(entry) if compute_size else None
        summaries.append(ProjectSummary(
            name=entry.name,
            path=str(entry),
            git=git,
            size_bytes=size,
            last_modified=mtime,
        ))
    return summaries


def human_age(mtime: float | None) -> str:
    if mtime is None:
        return "unknown"
    delta = time.time() - mtime
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
