import pytest

from home_services.auth import TokenStore
from home_services.config import ConfigError, load_config
from home_services.db import Database
from home_services.errors import Forbidden, Unauthenticated
from home_services.network import BindRefused, classify, resolve_bind_addresses, wait_for_bind_addresses
from home_services.runtime import FixedClock, SequentialIds

BASE = """
environment = "{env}"
data_dir = "data"
[mounts]
probe = "{probe}"
fixture_table = "m.json"
"""


def write(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return p


def test_literal_secret_refused(tmp_path):
    p = write(tmp_path, BASE.format(env="development", probe="fixture") + '[notifications]\nntfy_url = "https://ntfy.sh/x"\n')
    with pytest.raises(ConfigError, match="literal secret"):
        load_config(p)


def test_fixture_probe_refused_in_production(tmp_path):
    with pytest.raises(ConfigError, match="only allowed"):
        load_config(write(tmp_path, BASE.format(env="production", probe="fixture")))


def test_volume_requires_uuid_and_non_overlapping_roots(tmp_path):
    v = '[[volumes]]\nid="a"\nmount="/mnt/a"\nuuid="{uuid}"\n[volumes.roots]\nfiles="x"\nstaging="{staging}"\n'
    with pytest.raises(ConfigError, match="uuid is required"):
        load_config(write(tmp_path, BASE.format(env="development", probe="fixture") + v.format(uuid="", staging="y")))
    with pytest.raises(ConfigError, match="overlap"):
        load_config(write(tmp_path, BASE.format(env="development", probe="fixture") + v.format(uuid="U", staging="x/stage")))


def test_staging_must_share_files_volume(tmp_path):
    text = BASE.format(env="development", probe="fixture") + (
        '[[volumes]]\nid="a"\nmount="/mnt/a"\nuuid="A"\n[volumes.roots]\nfiles="f"\n'
        '[[volumes]]\nid="b"\nmount="/mnt/b"\nuuid="B"\n[volumes.roots]\nstaging="s"\n')
    with pytest.raises(ConfigError, match="same volume"):
        load_config(write(tmp_path, text))


def test_camera_engine_must_be_known_and_have_a_playlist_source(tmp_path):
    base = BASE.format(env="development", probe="fixture")
    with pytest.raises(ConfigError, match="not supported"):
        load_config(write(tmp_path, base + '[cameras]\nengine="frigate"\n'))
    with pytest.raises(ConfigError, match="live .m3u8"):
        load_config(write(tmp_path, base + '[cameras]\nengine="hls-recorder"\n[[cameras.devices]]\nid="c"\nname="C"\nsource="/dev/video0"\n'))
    cfg = load_config(write(tmp_path, base + '[cameras]\nengine="hls-recorder"\n[[cameras.devices]]\nid="c"\nname="C"\n'
                                             'source="/run/cam/index.m3u8"\n'))
    assert cfg.cameras.engine == "hls-recorder" and cfg.cameras.devices[0].source == "/run/cam/index.m3u8"


@pytest.mark.parametrize("addr,kind", [("127.0.0.1", "loopback"), ("100.101.102.103", "tailscale"), ("192.168.1.20", "lan"),
                                       ("fd7a:115c:a1e0::1", "tailscale"), ("::1", "loopback")])
def test_private_addresses_accepted(addr, kind):
    assert classify(addr) == kind


@pytest.mark.parametrize("addr", ["0.0.0.0", "::", "8.8.8.8", "169.254.1.1"])
def test_public_and_wildcard_refused(addr):
    with pytest.raises(BindRefused):
        classify(addr)


def test_interface_public_addresses_are_skipped_not_bound():
    lookup = {"eth0": ["203.0.113.5", "192.168.1.20"]}.get
    assert resolve_bind_addresses(("127.0.0.1",), ("eth0",), lambda n: lookup(n) or []) == ["127.0.0.1", "192.168.1.20"]


def test_wait_for_interface_times_out_instead_of_falling_back():
    with pytest.raises(BindRefused, match="not binding anything broader"):
        wait_for_bind_addresses((), ("tailscale",), timeout=0, lookup=lambda n: [], sleep=lambda s: None)


def test_wait_returns_once_interface_appears():
    calls = {"n": 0}

    def lookup(_):
        calls["n"] += 1
        return ["100.64.0.7"] if calls["n"] > 2 else []

    assert wait_for_bind_addresses((), ("tailscale",), timeout=60, lookup=lookup, sleep=lambda s: None) == ["100.64.0.7"]


def test_tokens_hashed_scoped_revocable_expiring(tmp_path):
    clock = FixedClock(1000)
    db = Database(tmp_path / "db.sqlite3")
    store = TokenStore(db, clock, SequentialIds())
    token, info = store.create("phone", ["storage:read"], ttl_days=1)
    secret = token.split(".", 1)[1]
    assert secret not in (tmp_path / "db.sqlite3").read_bytes().decode("latin-1")
    p = store.authenticate(token)
    p.require("storage:read")
    with pytest.raises(Forbidden):
        p.require("storage:write")
    with pytest.raises(Unauthenticated):
        store.authenticate(token[:-1] + "x")
    with pytest.raises(Unauthenticated):
        store.authenticate(None)
    clock.advance(86401)
    with pytest.raises(Unauthenticated, match="expired"):
        store.authenticate(token)
    token2, info2 = store.create("other", ["jobs:read"])
    store.revoke(info2["id"])
    with pytest.raises(Unauthenticated, match="revoked"):
        store.authenticate(token2)
    with pytest.raises(ValueError):
        store.create("bad", ["root:everything"])


def test_production_example_config_loads():
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parents[1] / "deploy" / "config.example.toml")
    assert cfg.environment == "production" and cfg.mount_probe == "linux"
    assert cfg.volume_for_role("restic_repository").mount == "/mnt/workspace-backup"
    assert cfg.backup.retention.restic_args() == ["--keep-hourly", "24", "--keep-daily", "14",
                                                  "--keep-weekly", "8", "--keep-monthly", "12"]


def test_state_store_refused_on_exfat_or_unknown(env):
    import json
    from home_services.volumes import VolumeGuard
    t = json.loads(env.table.read_text())
    for fstype in ("exfat", None):
        t["__data_dir_fstype__"] = {"fstype": fstype}
        env.table.write_text(json.dumps(t))
        assert VolumeGuard(env.config).data_dir_filesystem_ok()[0] is False
