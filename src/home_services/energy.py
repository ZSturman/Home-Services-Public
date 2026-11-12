"""Read-only Tapo history and bill allocation, independent of the API/worker.

Run with ``python -m home_services.energy --help`` in the optional energy venv.
Gmail access and delivery belong to the scheduled chat, never this module.
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import html
import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


def number(value, *, positive=False) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("Expected a finite positive value" if positive else "Expected a finite nonnegative value")
    return result


def validate_bill(bill: dict) -> dict:
    start = date.fromisoformat(bill["period_start"])
    end = date.fromisoformat(bill["period_end_exclusive"])
    if end <= start:
        raise ValueError("Bill end must follow start")
    number(bill["usage_kwh"], positive=True)
    number(bill["new_charges_usd"])
    if not bill.get("source"):
        raise ValueError("Bill source is required")
    return bill


def quarter_starts(start: date, end: date) -> list[date]:
    current = date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
    result = []
    while current < end:
        result.append(current)
        current = date(current.year + 1, 1, 1) if current.month == 10 else date(current.year, current.month + 3, 1)
    return result


def daily_values(payload: dict, start: date, end: date, timezone: str) -> dict[str, int]:
    """Use returned timestamps, never array offsets or fabricated missing zeros."""
    if payload.get("interval_length") != 1440:
        raise ValueError("Expected daily Wh records")
    zone = ZoneInfo(timezone)
    values = {}
    for entry in payload["entries"]:
        stamp = datetime.fromisoformat(entry["start_date_time"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Energy timestamps must include a timezone")
        day = stamp.astimezone(zone).date()
        if not start <= day < end or entry["energy"] is None:
            continue
        value = number(entry["energy"])
        if value != value.to_integral_value():
            raise ValueError("Expected whole Wh")
        key = day.isoformat()
        if key in values:
            raise ValueError("Duplicate daily energy record")
        values[key] = int(value)
    return values


async def collect(env_file: Path, start: date, end: date, timezone: str) -> list[dict]:
    from dotenv import dotenv_values
    from tapo import ApiClient
    from tapo.requests import EnergyDataInterval

    # Keep process dates consistent; tapo also reads each plug's timezone.
    os.environ["TZ"] = timezone
    time.tzset()
    config = dotenv_values(env_file)
    username, password = config.get("TAPO_USERNAME"), config.get("TAPO_PASSWORD")
    if not username or not password:
        raise ValueError("TAPO_USERNAME and TAPO_PASSWORD must be configured in the private env file")
    devices = []
    for item in (config.get("TAPO_DEVICES") or "").split(","):
        name, sep, ip = item.partition("=")
        if not sep or not name.strip() or not ip.strip():
            raise ValueError("TAPO_DEVICES must contain Name=IP entries")
        devices.append((name.strip(), ip.strip()))
    if len({ip for _, ip in devices}) != len(devices):
        raise ValueError("Duplicate plug addresses would double-count energy")
    client = ApiClient(username, password)
    result = []
    for label, ip in devices:
        record = {"configured_name": label, "name": label, "ip": ip, "daily_wh": {}, "errors": []}
        try:
            async with asyncio.timeout(15):
                device = await client.p110(ip)
                info = (await device.get_device_info()).to_dict()
                record["name"] = info.get("nickname", label).strip() or label
                record["model"] = info.get("model")
            for quarter in quarter_starts(start, end):
                try:
                    async with asyncio.timeout(15):
                        payload = (await device.get_energy_data(EnergyDataInterval.Daily, quarter)).to_dict()
                        values = daily_values(payload, start, end, timezone)
                        if record["daily_wh"].keys() & values.keys():
                            raise ValueError("Overlapping quarterly responses")
                        record["daily_wh"].update(values)
                except Exception as exc:
                    record["errors"].append(f"History {quarter}: {type(exc).__name__}; data unavailable")
        except Exception as exc:
            # Library exceptions may contain request details: never log them.
            record["errors"].append(f"{type(exc).__name__}; plug unavailable, check address and credentials")
        result.append(record)
    return result


def build_report(bill: dict, records: list[dict], start: date, end: date, timezone: str) -> dict:
    validate_bill(bill)
    if end <= start:
        raise ValueError("Report end must follow start")
    rate = number(bill["new_charges_usd"]) / number(bill["usage_kwh"], positive=True)
    expected = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days)}
    devices = []
    for record in records:
        days = record["daily_wh"]
        if not days.keys() <= expected:
            raise ValueError("Daily values outside report period")
        kwh = sum((number(v) for v in days.values()), Decimal(0)) / 1000 if days else None
        complete = days.keys() == expected and not record["errors"]
        devices.append({**record, "reported_days": len(days), "expected_days": len(expected),
                        "complete": complete, "kwh": str(kwh) if kwh is not None else None,
                        "estimated_cost_usd": str((kwh * rate).quantize(Decimal("0.01"))) if kwh is not None else None})
    total = sum((Decimal(d["kwh"]) for d in devices if d["kwh"] is not None), Decimal(0))
    return {
        "generated_at": datetime.now(ZoneInfo(timezone)).isoformat(),
        "period_start": start.isoformat(), "period_end_exclusive": end.isoformat(),
        "timezone": timezone, "bill": bill, "average_bill_usd_per_kwh": str(rate),
        "devices": devices, "all_devices_complete": bool(devices) and all(d["complete"] for d in devices),
        "known_kwh": str(total), "estimated_known_cost_usd": str((total * rate).quantize(Decimal("0.01"))),
        "cost_basis": "Estimated allocation at new electricity charges / billed kWh. Includes any fixed fees and taxes; not marginal savings or an itemized tariff.",
        "limitations": [
            "Missing days and offline plugs are unknown, not zero. Partial totals are not extrapolated.",
            "Plug history reports device totals, not the individual appliances on a shared power strip.",
            "Returned zero days do not prove the plug was installed or recording for the entire day.",
            "Calendar-month usage uses the latest supplied bill rate; its dates may differ from the billing period.",
            "Hue lights are excluded until a separate state-history collector and bulb power models are configured.",
        ],
    }


def render_html(report: dict) -> str:
    esc = html.escape
    rows = []
    for d in report["devices"]:
        status = "Reported" if d["complete"] else f'Incomplete ({d["reported_days"]}/{d["expected_days"]} days)'
        kwh = f'{Decimal(d["kwh"]):.3f}' if d["kwh"] is not None else "Unavailable"
        cost = f'${d["estimated_cost_usd"]}' if d["estimated_cost_usd"] is not None else "Unavailable"
        rows.append(f'<tr><td>{esc(d["name"])}</td><td>{kwh}</td><td>{cost}</td><td>{status}</td></tr>')
    last_day = date.fromisoformat(report["period_end_exclusive"]) - timedelta(days=1)
    rate = Decimal(report["average_bill_usd_per_kwh"]) * 100
    source = report["bill"]["source"]
    return f'''<!doctype html><html><body style="font:16px system-ui;color:#17202a;max-width:800px;margin:auto;padding:24px">
<h1>Monthly device energy report</h1>
<p>{report["period_start"]} through {last_day} · {esc(report["timezone"])}</p>
<p>Latest bill: ${esc(str(report["bill"]["new_charges_usd"]))} / {esc(str(report["bill"]["usage_kwh"]))} kWh = <b>{rate:.2f}¢/kWh</b> average.</p>
<table cellpadding="10" style="border-collapse:collapse;text-align:left" border="1"><tr><th>Plug / device</th><th>kWh</th><th>Estimated cost</th><th>Data</th></tr>{"".join(rows)}</table>
<p><b>Known usage: {Decimal(report["known_kwh"]):.3f} kWh · estimated ${report["estimated_known_cost_usd"]}</b><br>
{"All configured plugs returned daily records." if report["all_devices_complete"] else "Incomplete total: some plug data is unavailable."}</p>
<p>{esc(report["cost_basis"])}</p><p>Bill source: {esc(source)}<br>
Bill interval: {report["bill"]["period_start"]} to {report["bill"]["period_end_exclusive"]} (end exclusive).</p>
<ul>{"".join("<li>" + esc(s) + "</li>" for s in report["limitations"])}</ul>
</body></html>'''


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--bill", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Private output prefix; writes .json and .html")
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--month", help="Calendar YYYY-MM; defaults to the supplied bill interval")
    args = parser.parse_args()
    bill = validate_bill(json.loads(args.bill.read_text()))
    if args.month:
        start = date.fromisoformat(args.month + "-01")
        end = start + timedelta(days=calendar.monthrange(start.year, start.month)[1])
    else:
        start = date.fromisoformat(bill["period_start"])
        end = date.fromisoformat(bill["period_end_exclusive"])
    if end > datetime.now(ZoneInfo(args.timezone)).date():
        parser.error("Only completed local days can be reported; retry after the period ends")
    records = asyncio.run(collect(args.env_file, start, end, args.timezone))
    report = build_report(bill, records, start, end, args.timezone)
    private_write(args.output.with_suffix(".json"), json.dumps(report, indent=2) + "\n")
    private_write(args.output.with_suffix(".html"), render_html(report))
    print(json.dumps({"report": str(args.output.with_suffix(".json")), "complete": report["all_devices_complete"],
                      "known_kwh": report["known_kwh"], "estimated_known_cost_usd": report["estimated_known_cost_usd"]}))
    return 0  # Missing devices are explicitly represented; a useful partial report can be emailed.


if __name__ == "__main__":
    raise SystemExit(main())
