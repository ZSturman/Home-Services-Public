from home_services.observe import summarize


def sample(ts, group, active="active"):
    return {
        "ts": ts, "at": str(ts),
        group: {"camera-producer": {"active": active, "restarts": 0}},
        "backups": [],
    }


def test_owned_compatibility_units_are_monitored_after_retirement():
    _, problems = summarize([sample(1, "compatibility", "failed")])
    assert any("camera-producer not always active" in p for p in problems)


def test_retirement_preserves_reports_spanning_old_and_new_observation_schema():
    lines, problems = summarize([sample(1, "predecessor"), sample(2, "compatibility")])
    assert not problems
    assert sum("camera-producer: active 100%" in line for line in lines) == 2
