"""Restricted file browsing for the dashboard's Files view.

Every function here takes an absolute path that has ALREADY been validated
by paths.resolve_within_roots / join_within_root -- this module does not
re-derive trust, it assumes the caller (dashboard API layer) did the
containment check. Keep it that way: one choke point, not two.
"""
from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

TEXT_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".sh", ".swift", ".go", ".rs", ".rb", ".c", ".h",
    ".cpp", ".hpp", ".java", ".kt", ".html", ".css", ".xml", ".gitignore",
    ".env.example", ".sql", ".graphql", ".proto",
}
MAX_TEXT_PREVIEW_BYTES = 512_000  # 500KB -- big enough for source files, not for logs/dumps


@dataclass
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size_bytes: int | None
    modified: float


@dataclass
class FileContent:
    path: str
    text: str
    truncated: bool


def list_directory(path: Path) -> list[FileEntry]:
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith("."):
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(FileEntry(
            name=child.name,
            path=str(child),
            is_dir=child.is_dir(),
            size_bytes=None if child.is_dir() else stat.st_size,
            modified=stat.st_mtime,
        ))
    return entries


def is_previewable_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS or path.name in ("README", "LICENSE", "Makefile")


def read_text_preview(path: Path) -> FileContent:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if not is_previewable_text(path):
        raise ValueError(f"{path.name} is not a previewable text type")
    data = path.read_bytes()
    truncated = len(data) > MAX_TEXT_PREVIEW_BYTES
    data = data[:MAX_TEXT_PREVIEW_BYTES]
    text = data.decode("utf-8", errors="replace")
    return FileContent(path=str(path), text=text, truncated=truncated)


def guess_mimetype(path: Path) -> str:
    mtype, _ = mimetypes.guess_type(str(path))
    return mtype or "application/octet-stream"
