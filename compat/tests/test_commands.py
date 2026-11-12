import json

import pytest

from workspace_util.commands import (
    CommandHistory,
    CommandNotAllowedError,
    run_whitelisted_command,
)
from workspace_util.config import CommandSpec


def test_run_allowed_command_succeeds(tmp_path):
    project = tmp_path / "Projects" / "demo"
    project.mkdir(parents=True)
    spec = CommandSpec(name="echo_test", command=["echo", "hello"], allowed_roots=[str(tmp_path / "Projects")])
    result = run_whitelisted_command(spec, str(project))
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_run_command_rejects_cwd_outside_allowed_roots(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    spec = CommandSpec(name="echo_test", command=["echo", "hi"], allowed_roots=[str(tmp_path / "Projects")])
    with pytest.raises(CommandNotAllowedError):
        run_whitelisted_command(spec, str(outside))


def test_run_command_rejects_traversal_cwd(tmp_path):
    (tmp_path / "Projects").mkdir()
    spec = CommandSpec(name="echo_test", command=["echo", "hi"], allowed_roots=[str(tmp_path / "Projects")])
    traversal_cwd = str(tmp_path / "Projects" / ".." / "..")
    with pytest.raises(CommandNotAllowedError):
        run_whitelisted_command(spec, traversal_cwd)


def test_run_command_rejects_nonexistent_cwd(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    spec = CommandSpec(name="echo_test", command=["echo", "hi"], allowed_roots=[str(root)])
    with pytest.raises(CommandNotAllowedError):
        run_whitelisted_command(spec, str(root / "does-not-exist"))


def test_command_never_invoked_with_shell(tmp_path, monkeypatch):
    """Defense in depth: assert the subprocess call never sets shell=True,
    even if a future edit accidentally tries to build a shell string."""
    import workspace_util.commands as commands_mod

    captured = {}

    def fake_run(argv, cwd, capture_output, text, timeout, shell):
        captured["shell"] = shell
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()

    monkeypatch.setattr(commands_mod.subprocess, "run", fake_run)
    root = tmp_path / "Projects"
    root.mkdir()
    spec = CommandSpec(name="git_status", command=["git", "status"], allowed_roots=[str(root)])
    run_whitelisted_command(spec, str(root))
    assert captured["shell"] is False
    assert captured["argv"] == ["git", "status"]


def test_command_times_out(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    spec = CommandSpec(name="sleeper", command=["sleep", "5"], allowed_roots=[str(root)], timeout_seconds=1)
    result = run_whitelisted_command(spec, str(root))
    assert result.timed_out is True
    assert result.exit_code is None


def test_command_history_records_and_reads_back(tmp_path):
    history = CommandHistory(tmp_path / "history.jsonl")
    root = tmp_path
    spec = CommandSpec(name="echo_test", command=["echo", "hi"], allowed_roots=[str(root)])
    result = run_whitelisted_command(spec, str(root))
    history.record(result)
    recent = history.recent(limit=10)
    assert len(recent) == 1
    assert recent[0]["command_name"] == "echo_test"


def test_command_history_survives_malformed_lines(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text("not json\n" + json.dumps({"command_name": "ok"}) + "\n")
    history = CommandHistory(path)
    recent = history.recent(limit=10)
    assert len(recent) == 1
    assert recent[0]["command_name"] == "ok"
