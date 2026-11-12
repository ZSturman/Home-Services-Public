import pytest

from workspace_util.paths import PathNotAllowedError, join_within_root, resolve_within_roots


def test_resolve_within_roots_allows_path_inside_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "Projects" / "demo"
    target.mkdir(parents=True)
    resolved = resolve_within_roots(str(target), [str(root)])
    assert resolved == target.resolve()


def test_resolve_within_roots_rejects_outside_path(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(PathNotAllowedError):
        resolve_within_roots(str(outside), [str(root)])


def test_resolve_within_roots_rejects_dotdot_traversal(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    traversal = str(root / ".." / "secret.txt")
    with pytest.raises(PathNotAllowedError):
        resolve_within_roots(traversal, [str(root)])


def test_resolve_within_roots_rejects_symlink_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    link = root / "escape"
    link.symlink_to(outside)
    with pytest.raises(PathNotAllowedError):
        resolve_within_roots(str(link / "secret.txt"), [str(root)])


def test_resolve_within_roots_no_roots_configured(tmp_path):
    with pytest.raises(PathNotAllowedError):
        resolve_within_roots(str(tmp_path), [])


def test_join_within_root_rejects_absolute_path(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(PathNotAllowedError):
        join_within_root(str(root), "/etc/passwd")


def test_join_within_root_rejects_dotdot(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(PathNotAllowedError):
        join_within_root(str(root), "../secret.txt")


def test_join_within_root_allows_nested_relative_path(tmp_path):
    root = tmp_path / "workspace"
    (root / "Projects").mkdir(parents=True)
    result = join_within_root(str(root), "Projects/demo.txt")
    assert result == (root / "Projects" / "demo.txt").resolve()
