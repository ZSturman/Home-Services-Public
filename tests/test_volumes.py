import os
from pathlib import Path

import pytest

from home_services.errors import PathNotAllowed, VolumeUnavailable
from home_services.volumes import LinuxMountProbe, contained


def mountinfo_line(mount: Path, device: str, fstype: str) -> str:
    st = os.stat(mount).st_dev
    return f"36 25 {os.major(st)}:{os.minor(st)} / {mount} rw,relatime shared:1 - {fstype} {device} rw\n"


def test_linux_probe_matches_device_and_uuid(tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    dev = tmp_path / "sda2"
    dev.write_text("")
    by_uuid = tmp_path / "by-uuid"
    by_uuid.mkdir()
    (by_uuid / "ABCD-1234").symlink_to(dev)
    mi = tmp_path / "mountinfo"
    mi.write_text("22 1 0:1 / / rw - ext4 /dev/root rw\n" + mountinfo_line(mount.resolve(), str(dev), "exfat"))
    probe = LinuxMountProbe(str(mi), str(by_uuid))
    entry = probe.entry_for(str(mount))
    assert entry and entry.uuid == "ABCD-1234" and entry.fstype == "exfat"
    assert probe.fstype_for_path(str(mount / "x")) == "exfat"
    assert probe.fstype_for_path("/var/lib/home-services") == "ext4"


def test_linux_probe_rejects_plain_directory_and_stale_device(tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    mi = tmp_path / "mountinfo"
    mi.write_text("22 1 0:1 / / rw - ext4 /dev/root rw\n")
    assert LinuxMountProbe(str(mi), str(tmp_path)).entry_for(str(mount)) is None
    # Listed, but the path's st_dev differs from the listed major:minor.
    mi.write_text(f"36 25 250:250 / {mount.resolve()} rw - exfat /dev/sdz1 rw\n")
    assert LinuxMountProbe(str(mi), str(tmp_path)).entry_for(str(mount)) is None


def test_guard_missing_and_wrong_disk_fail_closed(env):
    assert env.s.guard.root_path("files", write=True) == env.files.resolve()
    env.unplug()
    with pytest.raises(VolumeUnavailable) as e:
        env.s.guard.root_path("files", write=True)
    assert e.value.details["state"] == "missing"
    env.wrong_disk()
    with pytest.raises(VolumeUnavailable) as e:
        env.s.guard.root_path("files", write=True)
    assert e.value.details["state"] == "wrong_volume"


def test_unmounted_mountpoint_writability_is_reported(env):
    env.unplug()
    st = env.s.guard.status(env.config.volume("backup-hdd"))
    assert st.state == "missing" and st.unmounted_mountpoint_writable is True


def test_repository_and_legacy_roots_are_never_writable(env):
    for role in ("restic_repository", "legacy_media"):
        with pytest.raises(PathNotAllowed):
            env.s.guard.root_path(role, write=True)
        env.s.guard.root_path(role, write=False)


def test_missing_root_is_not_created_implicitly(env):
    (env.files).rename(env.mount / "moved")
    with pytest.raises(VolumeUnavailable) as e:
        env.s.guard.root_path("files", write=True)
    assert e.value.details["state"] == "root_missing"
    assert not env.files.exists()


def test_root_symlinked_off_volume_rejected(env, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("sdcard")
    env.files.rename(env.mount / "moved")
    env.files.symlink_to(elsewhere)
    with pytest.raises(VolumeUnavailable):
        env.s.guard.root_path("files", write=True)


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "a\x00b"])
def test_contained_rejects_traversal(tmp_path, bad):
    with pytest.raises(PathNotAllowed):
        contained(tmp_path, bad)


def test_contained_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(PathNotAllowed):
        contained(root.resolve(), "link/secret")


def test_move_rename_conflicts_and_boundaries(env):
    from home_services.errors import Conflict, NotFound, PathNotAllowed, VolumeUnavailable
    s = env.s.storage
    (env.files / "a").mkdir()
    (env.files / "a/x.txt").write_text("x")
    (env.files / "b.txt").write_text("b")
    moved = s.move("a/x.txt", "y.txt")
    assert moved["from"] == "a/x.txt" and moved["path"] == "y.txt" and (env.files / "y.txt").read_text() == "x"
    with pytest.raises(Conflict) as e:
        s.move("y.txt", "b.txt")
    assert e.value.code == "destination_exists"
    assert s.move("y.txt", "b.txt", conflict="keep_both")["path"] == "b (1).txt"
    assert s.move("b.txt", "B.txt")["path"] == "B.txt"  # case-only rename works on either kind of volume
    with pytest.raises(PathNotAllowed):
        s.move("a", "a/inner")
    with pytest.raises(PathNotAllowed):
        s.move("B.txt", "../../restic-repo/B.txt")
    with pytest.raises(NotFound):
        s.move("B.txt", "missing/B.txt")
    env.unplug()
    with pytest.raises(VolumeUnavailable):
        s.move("B.txt", "C.txt")
