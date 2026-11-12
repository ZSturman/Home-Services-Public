import subprocess
from pathlib import Path

from workspace_util.git_scan import scan_repo


def _git(repo: Path, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path):
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")


def test_scan_non_repo_directory(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    state = scan_repo(d)
    assert state.is_repo is False
    assert state.branch is None


def test_scan_clean_repo(tmp_path):
    repo = tmp_path / "clean-repo"
    _init_repo(repo)
    (repo / "README.md").write_text("hello")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")

    state = scan_repo(repo)
    assert state.is_repo is True
    assert state.branch == "main"
    assert state.clean is True
    assert state.modified_count == 0
    assert state.untracked_count == 0
    assert state.last_commit_iso is not None


def test_scan_repo_with_modifications_and_untracked(tmp_path):
    repo = tmp_path / "dirty-repo"
    _init_repo(repo)
    (repo / "README.md").write_text("hello")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")

    (repo / "README.md").write_text("modified")
    (repo / "new_file.txt").write_text("new")

    state = scan_repo(repo)
    assert state.clean is False
    assert state.modified_count == 1
    assert state.untracked_count == 1
    assert any("synced machine" in w for w in state.warnings)


def test_scan_repo_never_mutates_repo_state(tmp_path):
    """git_scan is read-only: running it must not stage, commit, or stash."""
    repo = tmp_path / "readonly-check"
    _init_repo(repo)
    (repo / "README.md").write_text("hello")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    (repo / "README.md").write_text("modified")

    scan_repo(repo)

    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert "M README.md" in status.stdout or " M README.md" in status.stdout
