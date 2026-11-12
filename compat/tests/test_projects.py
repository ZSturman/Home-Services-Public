import subprocess
from pathlib import Path

from workspace_util.projects import discover_projects, human_age


def _init_repo(repo: Path):
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, capture_output=True)


def test_discover_projects_empty_root(tmp_path):
    assert discover_projects(tmp_path / "does-not-exist") == []


def test_discover_projects_skips_dotfiles_and_finds_repos(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    _init_repo(root / "Alpha")
    (root / ".hidden").mkdir()
    (root / "NotARepo").mkdir()

    summaries = discover_projects(root)
    names = {s.name for s in summaries}
    assert names == {"Alpha", "NotARepo"}
    alpha = next(s for s in summaries if s.name == "Alpha")
    assert alpha.git.is_repo is True
    not_repo = next(s for s in summaries if s.name == "NotARepo")
    assert not_repo.git.is_repo is False


def test_discover_projects_size_computation(tmp_path):
    root = tmp_path / "Projects"
    project = root / "Sized"
    project.mkdir(parents=True)
    (project / "a.txt").write_bytes(b"x" * 1000)
    (project / "node_modules").mkdir()
    (project / "node_modules" / "big.bin").write_bytes(b"y" * 5000)

    summaries = discover_projects(root, compute_size=True)
    sized = summaries[0]
    # node_modules must be excluded from the size total (skip_dirs)
    assert sized.size_bytes == 1000


def test_human_age_buckets():
    import time
    now = time.time()
    assert human_age(None) == "unknown"
    assert human_age(now - 5) == "just now"
    assert human_age(now - 120) == "2m ago"
    assert human_age(now - 7200) == "2h ago"
    assert human_age(now - 172800) == "2d ago"
