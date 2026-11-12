import time

from workspace_util.config import (
    BackupConfig, Config, DashboardConfig, DiskConfig, LoggingConfig,
    MacBridgeConfig, NotificationsConfig, SyncConfig, TapoConfig, TapoDeviceConfig, WorkspaceConfig,
)
from workspace_util.tapo import PowerReading, TapoService, database_path, extract_watts


def make_config(tmp_path, **tapo_overrides) -> Config:
    defaults = dict(
        enabled=True,
        devices=[TapoDeviceConfig(name="Office Lamp", ip="10.0.0.50")],
    )
    defaults.update(tapo_overrides)
    return Config(
        workspace=WorkspaceConfig(local_mac_path=str(tmp_path / "mac"), pi_path=str(tmp_path / "pi")),
        disk=DiskConfig(), sync=SyncConfig(), backup=BackupConfig(),
        dashboard=DashboardConfig(), mac_bridge=MacBridgeConfig(),
        notifications=NotificationsConfig(backends=[{"type": "log"}]),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
        commands={}, source_path=tmp_path / "config.yaml",
        tapo=TapoConfig(**defaults),
    )


def test_extract_watts_handles_known_field_names():
    assert extract_watts({"current_power": 12.5}) == 12.5
    assert extract_watts({"current_power_w": 7}) == 7.0
    assert extract_watts({"power": 3}) == 3.0
    assert extract_watts({"current_power_mw": 1500}) == 1.5
    assert extract_watts({"unrelated": 1}) is None
    assert extract_watts({}) is None


def test_database_path_defaults_under_logging_dir(tmp_path):
    from pathlib import Path

    cfg = make_config(tmp_path)
    assert database_path(cfg) == Path(cfg.logging.dir) / "tapo_power.sqlite3"


def test_database_path_honors_explicit_override(tmp_path):
    cfg = make_config(tmp_path, database_path=str(tmp_path / "custom.sqlite3"))
    assert str(database_path(cfg)) == str(tmp_path / "custom.sqlite3")


def test_status_reports_no_reading_yet_for_unpolled_device(tmp_path):
    cfg = make_config(tmp_path)
    service = TapoService(cfg)
    from workspace_util.tapo import init_db
    init_db(cfg)

    status = service.status()
    assert status["enabled"] is True
    assert status["devices"] == [{
        "name": "Office Lamp", "ip": "10.0.0.50", "watts": None,
        "device_on": None, "error": "no reading yet", "last_seen_at": None,
    }]


def test_record_and_history_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    service = TapoService(cfg)
    from workspace_util.tapo import init_db
    init_db(cfg)

    now = time.time()
    reading = PowerReading(device_name="Office Lamp", ip="10.0.0.50", watts=42.0, device_on=True, error=None, last_seen_at=now)
    service._record(reading, now)

    history = service.history("Office Lamp", hours=1)
    assert len(history) == 1
    assert history[0]["watts"] == 42.0
    assert history[0]["device_on"] is True


def test_record_persists_device_status_even_without_live_cache(tmp_path):
    cfg = make_config(tmp_path)
    service = TapoService(cfg)
    from workspace_util.tapo import init_db
    init_db(cfg)

    now = time.time()
    reading = PowerReading(device_name="Office Lamp", ip="10.0.0.50", watts=15.5, device_on=False, error=None, last_seen_at=now)
    service._record(reading, now)

    # A fresh service instance (simulating a dashboard restart) must still
    # see the last known reading via device_status, not just in-memory cache.
    fresh_service = TapoService(cfg)
    status = fresh_service.status()
    assert status["devices"][0]["watts"] == 15.5
    assert status["devices"][0]["device_on"] is False
    assert status["devices"][0]["error"] is None


def test_summary_computes_avg_peak_and_estimated_kwh(tmp_path):
    cfg = make_config(tmp_path)
    service = TapoService(cfg)
    from workspace_util.tapo import init_db
    init_db(cfg)

    now = time.time()
    for watts in (10.0, 20.0, 30.0):
        reading = PowerReading(device_name="Office Lamp", ip="10.0.0.50", watts=watts, device_on=True, error=None, last_seen_at=now)
        service._record(reading, now)

    summary = service.summary("Office Lamp", hours=1)
    assert summary["samples"] == 3
    assert summary["avg_watts"] == 20.0
    assert summary["peak_watts"] == 30.0
    assert summary["estimated_kwh"] == (20.0 * 1) / 1000.0


def test_summary_with_no_samples_reports_none(tmp_path):
    cfg = make_config(tmp_path)
    service = TapoService(cfg)
    from workspace_util.tapo import init_db
    init_db(cfg)

    summary = service.summary("Office Lamp", hours=1)
    assert summary == {
        "device_name": "Office Lamp", "hours": 1, "samples": 0,
        "avg_watts": None, "peak_watts": None, "estimated_kwh": None,
    }
