"""Recovery across explicitly configured repositories, with one writer per repo.

The primary adapter remains separate for writer receipts and retention previews.
Snapshot IDs route existing clients to the correct reader without credentials or
repository paths in API requests. A configured reader failure is never hidden.
"""
from __future__ import annotations

from ..errors import Conflict, NotFound
from .restic import Restic, safe_snapshot_id, snapshot_timestamp


class RecoveryCatalog:
    def __init__(self, primary: Restic):
        self.primary = primary
        self.readers = {"primary": primary}
        if primary.config.volume_for_role("companion_recovery") is not None:
            self.readers["companion"] = Restic(primary.config, primary.guard, role="companion_recovery")

    def snapshots(self, *, max_age: float = 0) -> list[dict]:
        if len(self.readers) == 1:
            return self.primary.snapshots(max_age=max_age)
        found = {}
        for name, reader in self.readers.items():
            for snap in reader.snapshots(max_age=max_age):
                found.setdefault(snap["id"], {**snap, "repository": name})
        return sorted(found.values(), key=lambda s: snapshot_timestamp(s["time"]), reverse=True)

    def resolve(self, snapshot_id: str) -> tuple[Restic, str]:
        snap_id = safe_snapshot_id(snapshot_id)
        if len(self.readers) == 1:
            return self.primary, snap_id
        matches = [s for s in self.snapshots(max_age=60) if s["id"].startswith(snap_id)]
        if not matches:
            raise NotFound("snapshot not found in configured recovery repositories")
        if len(matches) != 1:
            raise Conflict("snapshot prefix is ambiguous; use the full snapshot ID", code="ambiguous_snapshot_id")
        snap = matches[0]
        return self.readers[snap["repository"]], snap["id"]

    def list_dir(self, snapshot_id: str, path: str) -> list[dict]:
        reader, full_id = self.resolve(snapshot_id)
        return reader.list_dir(full_id, path)

    def open_dump(self, snapshot_id: str, path: str):
        reader, full_id = self.resolve(snapshot_id)
        return reader.open_dump(full_id, path)

    def restore(self, snapshot_id: str, includes: list[str], target, *, on_progress, should_stop) -> dict:
        reader, full_id = self.resolve(snapshot_id)
        return reader.restore(full_id, includes, target, on_progress=on_progress, should_stop=should_stop)
