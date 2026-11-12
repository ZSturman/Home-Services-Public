"""Path containment -- the single choke point every filesystem-touching
feature (file browser, command runner, restore) must call before opening,
listing, or executing anything derived from user/network input.
"""
from __future__ import annotations

from pathlib import Path


class PathNotAllowedError(PermissionError):
    pass


def resolve_within_roots(requested: str, allowed_roots: list[str]) -> Path:
    """Resolve `requested` (may be relative, may contain .., may be a symlink)
    and verify the *real* resolved path is inside one of allowed_roots.

    Raises PathNotAllowedError for anything outside the allowed roots,
    including traversal attempts and symlinks that escape the roots.
    """
    if not allowed_roots:
        raise PathNotAllowedError("no allowed roots configured")

    resolved_roots = [Path(r).resolve() for r in allowed_roots]

    try:
        candidate = Path(requested)
        if not candidate.is_absolute():
            # Relative paths are resolved against the first allowed root only
            # -- callers should already be passing an absolute path derived
            # from a known root plus a user-supplied relative fragment.
            candidate = resolved_roots[0] / candidate
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise PathNotAllowedError(f"could not resolve path: {exc}") from exc

    for root in resolved_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue

    raise PathNotAllowedError(f"{requested!r} resolves outside all allowed roots")


def join_within_root(root: str, relative: str) -> Path:
    """Join a user-supplied relative fragment onto a single trusted root and
    verify the result stays inside it. Rejects '..' traversal and absolute
    overrides even if the OS would otherwise honor them."""
    root_resolved = Path(root).resolve()
    # Reject absolute paths and any '..' component outright before joining,
    # rather than relying solely on post-hoc containment (defense in depth).
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise PathNotAllowedError(f"{relative!r} is not a valid relative path")
    candidate = (root_resolved / rel).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise PathNotAllowedError(f"{relative!r} resolves outside root {root}")
    return candidate
