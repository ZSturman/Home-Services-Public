import json
import os
import random
import subprocess
from datetime import datetime, timedelta

import pytest

from home_services.backups.retention import RetentionPolicy, apply_policy, preview

from conftest import HAS_RESTIC


def _snap(i, when):
    return {"id": f"{i:064x}", "time": when, "hostname": "h", "paths": ["/p"]}


def test_hourly_daily_buckets_and_oldest_kept():
    times = ["2026-10-20T10:50:00-04:00", "2026-10-20T10:05:00-04:00", "2026-10-20T09:30:00-04:00",
             "2026-10-19T23:00:00-04:00", "2026-10-19T08:00:00-04:00"]
    keep, remove = apply_policy([_snap(i, t) for i, t in enumerate(times)], RetentionPolicy(hourly=2, daily=5))
    assert [k["time"] for k in keep] == [times[0], times[2], times[3], times[4]]
    assert keep[0]["reasons"] == ["hourly snapshot", "daily snapshot"]
    assert keep[-1]["reasons"] == ["oldest daily snapshot"]  # same day as times[3], but daily counts are left
    assert [r["time"] for r in remove] == [times[1]]


def test_buckets_use_the_recorded_offset():
    # the same instant is 23:30 on the 19th in New York and 03:30 on the 20th in UTC
    b = _snap(2, "2026-10-20T03:40:00+00:00")
    a = _snap(1, "2026-10-19T23:30:00-04:00")
    c = _snap(3, "2026-10-19T22:00:00-04:00")
    d = _snap(4, "2026-10-19T21:00:00-04:00")
    keep, remove = apply_policy([a, b, c, d], RetentionPolicy(daily=5))
    assert [k["id"] for k in keep] == [b["id"], a["id"], d["id"]]  # a starts the 19th, not part of b's day
    assert [r["id"] for r in remove] == [c["id"]]


def test_preview_groups_by_host_and_paths_and_refuses_empty_policy():
    snaps = [_snap(1, "2026-10-20T10:00:00Z"), {**_snap(2, "2026-10-20T11:00:00Z"), "hostname": "other"},
             {**_snap(3, "2026-10-20T12:00:00Z"), "paths": ["/q", "/p"]}]
    out = preview(snaps, RetentionPolicy(last=1))
    assert [(g["host"], g["paths"]) for g in out["groups"]] == [("h", ["/p"]), ("h", ["/p", "/q"]), ("other", ["/p"])]
    assert out["totals"] == {"snapshots": 3, "keep": 3, "remove": 0}
    assert out["restic_equivalent"] == "restic forget --keep-last 1 --group-by host,paths"
    with pytest.raises(ValueError):
        preview(snaps, RetentionPolicy())


@pytest.mark.skipif(not HAS_RESTIC, reason="restic not installed")
def test_matches_restic_forget_dry_run(tmp_path):
    """Generated snapshots over two years, two hosts, and three UTC offsets;
    every policy must keep and remove exactly what restic says."""
    rng = random.Random(7)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").write_text("x")
    env = {**os.environ, "RESTIC_REPOSITORY": str(tmp_path / "repo"), "RESTIC_PASSWORD": "x",
           "RESTIC_CACHE_DIR": str(tmp_path / "cache")}
    subprocess.run(["restic", "init", "-q"], env=env, check=True)
    start = datetime(2024, 11, 1, 0, 0)
    used = set()
    for _ in range(70):
        # dense recent history, sparse old history, with some near-duplicates in one hour
        days = rng.choice([rng.uniform(0, 3), rng.uniform(0, 40), rng.uniform(0, 720)])
        when = (start + timedelta(days=720 - days)).replace(second=0, microsecond=0)
        if when in used:
            continue
        used.add(when)
        tz = rng.choice(["America/New_York", "UTC", "Asia/Kolkata"])
        host = rng.choice(["mac", "pi"])
        subprocess.run(["restic", "backup", "-q", "--host", host, "--time", when.strftime("%Y-%m-%d %H:%M:%S"), str(src)],
                       env={**env, "TZ": tz}, check=True, capture_output=True)
    listing = json.loads(subprocess.run(["restic", "snapshots", "--json", "--no-lock"], env=env, check=True,
                                        capture_output=True, text=True).stdout)
    snaps = [{"id": s["id"], "time": s["time"], "hostname": s["hostname"], "paths": s["paths"]} for s in listing]
    for policy in (RetentionPolicy(hourly=24, daily=14, weekly=8, monthly=12),  # the predecessor's policy
                   RetentionPolicy(last=3, daily=7), RetentionPolicy(weekly=4, yearly=3),
                   RetentionPolicy(hourly=5), RetentionPolicy(monthly=100)):
        args = " ".join(policy.restic_args()).split()
        proc = subprocess.run(["restic", "forget", "--dry-run", "--no-lock", "--json", *args], env=env, check=True,
                              capture_output=True, text=True)
        expected = {(g["host"], tuple(g["paths"])): ({s["id"] for s in g["keep"] or []}, {s["id"] for s in g["remove"] or []})
                    for g in json.loads(proc.stdout)}
        got = {(g["host"], tuple(g["paths"])): ({s["id"] for s in g["keep"]}, {s["id"] for s in g["remove"]})
               for g in preview(snaps, policy)["groups"]}
        assert got == expected, policy
