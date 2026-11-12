from datetime import date

import pytest

from home_services.energy import build_report, daily_values, private_write, quarter_starts, render_html


def bill():
    return {"period_start": "2026-09-28", "period_end_exclusive": "2026-10-28",
            "usage_kwh": "494", "new_charges_usd": "100.88", "source": "fixture"}


def test_quarters_include_year_boundary():
    assert quarter_starts(date(2026, 12, 31), date(2027, 1, 2)) == [date(2026, 10, 1), date(2027, 1, 1)]


def test_daily_timestamps_dst_missing_and_zero():
    payload = {"interval_length": 1440, "entries": [
        {"start_date_time": "2026-10-31T07:00:00Z", "energy": 200},
        {"start_date_time": "2026-11-01T07:00:00Z", "energy": 0},
        {"start_date_time": "2026-11-02T08:00:00Z", "energy": None},
        {"start_date_time": "2026-11-03T08:00:00Z", "energy": 900},
    ]}
    assert daily_values(payload, date(2026, 10, 31), date(2026, 11, 3), "America/Los_Angeles") == {
        "2026-10-31": 200, "2026-11-01": 0}


def test_bill_allocation_missing_plug_and_html_escape():
    records = [
        {"name": "Lamp <&>", "daily_wh": {"2026-10-01": 14766}, "errors": []},
        {"name": "Offline", "daily_wh": {}, "errors": ["TimeoutError"]},
    ]
    report = build_report(bill(), records, date(2026, 10, 1), date(2026, 10, 2), "America/Los_Angeles")
    assert report["devices"][0]["estimated_cost_usd"] == "3.02"
    assert report["devices"][1]["kwh"] is None
    assert report["known_kwh"] == "14.766"
    assert not report["all_devices_complete"]
    assert "Lamp &lt;&amp;&gt;" in render_html(report)
    assert "Unavailable" in render_html(report)
    assert "Incomplete total" in render_html(report)


@pytest.mark.parametrize("field,value", [("usage_kwh", 0), ("new_charges_usd", -1), ("new_charges_usd", "NaN")])
def test_invalid_bill(field, value):
    data = bill()
    data[field] = value
    with pytest.raises(ValueError):
        build_report(data, [], date(2026, 10, 1), date(2026, 11, 1), "America/Los_Angeles")


def test_duplicate_dates_rejected():
    entry = {"start_date_time": "2026-10-01T07:00:00Z", "energy": 1}
    with pytest.raises(ValueError, match="Duplicate"):
        daily_values({"interval_length": 1440, "entries": [entry, entry]}, date(2026, 10, 1), date(2026, 10, 2), "America/Los_Angeles")


def test_private_output_permissions(tmp_path):
    path = tmp_path / "report.json"
    private_write(path, "private")
    assert path.stat().st_mode & 0o777 == 0o600
    private_write(path, "updated")
    assert path.read_text() == "updated"
