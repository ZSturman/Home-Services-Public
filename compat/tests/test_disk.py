from workspace_util.disk import DiskInfo, list_candidate_external_disks


def _disk(**overrides):
    base = dict(
        device="/dev/sda1", model="Test Disk", capacity_bytes=5_000_000_000,
        filesystem="ext4", mountpoint="/mnt/workspace", uuid="1234", used_bytes=0,
        free_bytes=5_000_000_000, is_internal=False,
    )
    base.update(overrides)
    return DiskInfo(**base)


def test_human_bytes_formatting():
    d = _disk()
    assert d.human(None) == "-"
    assert d.human(500) == "500.0B"
    assert d.human(2048) == "2.0KB"
    assert d.human(5_000_000_000).endswith("GB")


def test_list_candidate_external_disks_excludes_internal(monkeypatch):
    import workspace_util.disk as disk_mod
    disks = [
        _disk(device="/dev/mmcblk0p2", is_internal=True, capacity_bytes=100_000_000_000),
        _disk(device="/dev/sda1", is_internal=False, capacity_bytes=1_000_000_000_000),
    ]
    monkeypatch.setattr(disk_mod, "list_disks", lambda: disks)
    candidates = list_candidate_external_disks()
    assert len(candidates) == 1
    assert candidates[0].device == "/dev/sda1"


def test_list_candidate_external_disks_excludes_tiny_volumes(monkeypatch):
    import workspace_util.disk as disk_mod
    disks = [_disk(device="/dev/sda1", is_internal=False, capacity_bytes=500_000)]  # 500KB, e.g. an EFI partition
    monkeypatch.setattr(disk_mod, "list_disks", lambda: disks)
    assert list_candidate_external_disks() == []
