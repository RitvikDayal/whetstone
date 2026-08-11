import subprocess
from pathlib import Path

import pytest

from whetstone.config.model import BoundariesConfig
from whetstone.errors import GitError
from whetstone.scope.resolver import is_write_forbidden, resolve_files


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    for rel in [
        "src/app.py",
        "src/generated/schema.py",
        "infra/main.tf",
        "docs/readme.md",
    ]:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init", "--no-gpg-sign")
    return tmp_path


def test_include_filters_to_declared_paths(repo):
    files = resolve_files(repo, BoundariesConfig(include=["src/**"]))
    assert files == (Path("src/app.py"), Path("src/generated/schema.py"))


def test_exclude_removes_paths(repo):
    files = resolve_files(
        repo, BoundariesConfig(include=["src/**"], exclude=["**/generated/**"])
    )
    assert files == (Path("src/app.py"),)


def test_never_touch_does_not_filter_analysis(repo):
    """never_touch is a write barrier. Findings there are still worth reporting."""
    files = resolve_files(
        repo, BoundariesConfig(include=["**/*"], never_touch=["infra/**"])
    )
    assert Path("infra/main.tf") in files


def test_is_write_forbidden_uses_never_touch():
    boundaries = BoundariesConfig(never_touch=["infra/**", ".github/workflows/**"])
    assert is_write_forbidden(Path("infra/main.tf"), boundaries)
    assert is_write_forbidden(Path(".github/workflows/ci.yml"), boundaries)
    assert not is_write_forbidden(Path("src/app.py"), boundaries)


def test_untracked_files_are_ignored(repo):
    (repo / "src" / "scratch.py").write_text("x", encoding="utf-8")
    files = resolve_files(repo, BoundariesConfig(include=["src/**"]))
    assert Path("src/scratch.py") not in files


def test_changed_only_narrows_to_the_diff(repo):
    _git(repo, "checkout", "-b", "feature")
    (repo / "src" / "app.py").write_text("changed", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "change", "--no-gpg-sign")
    files = resolve_files(
        repo, BoundariesConfig(include=["**/*"]), changed_only=True, base_branch="main"
    )
    assert files == (Path("src/app.py"),)


def test_results_are_sorted_and_deterministic(repo):
    """Pins the canonical POSIX-string order, not just idempotency.

    Sorting bare `Path` objects case-folds on Windows and compares byte-wise on
    POSIX, so `sorted(first)` re-run on already-sorted output would pass either
    way. This asserts the exact expected sequence for mixed-case names, which
    fails under case-folded ordering.
    """
    for rel in ["Zebra.py", "apple.py", "Beta.py"]:
        (repo / rel).write_text("x", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add mixed-case files", "--no-gpg-sign")
    boundaries = BoundariesConfig(include=["/Zebra.py", "/apple.py", "/Beta.py"])

    first = resolve_files(repo, boundaries)
    second = resolve_files(repo, boundaries)

    assert first == second == (Path("Beta.py"), Path("Zebra.py"), Path("apple.py"))


def _make_orphan_branch(repo: Path) -> None:
    """A branch with no shared history with `main` — `merge-base` has nothing to find."""
    _git(repo, "checkout", "--orphan", "feature")
    _git(repo, "rm", "-rf", "--cached", ".")
    (repo / "b.py").write_text("y", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "orphan", "--no-gpg-sign")


def test_changed_only_raises_when_orphan_branch_has_clean_tree(repo):
    """No common ancestor + clean tree used to silently resolve to zero files."""
    _make_orphan_branch(repo)
    with pytest.raises(GitError, match="main"):
        resolve_files(
            repo,
            BoundariesConfig(include=["**/*"]),
            changed_only=True,
            base_branch="main",
        )


def test_changed_only_raises_when_orphan_branch_has_dirty_tree(repo):
    """Same missing-ancestor case, but with uncommitted changes present."""
    _make_orphan_branch(repo)
    (repo / "b.py").write_text("dirty", encoding="utf-8")
    with pytest.raises(GitError, match="main"):
        resolve_files(
            repo,
            BoundariesConfig(include=["**/*"]),
            changed_only=True,
            base_branch="main",
        )


def test_changed_only_raises_when_base_branch_is_absent(repo):
    with pytest.raises(GitError, match="does-not-exist"):
        resolve_files(
            repo,
            BoundariesConfig(include=["**/*"]),
            changed_only=True,
            base_branch="does-not-exist",
        )


def test_deleted_tracked_files_are_filtered_out(repo):
    """A file removed from the working tree but still in the index has nothing
    to read. It is dropped from the analysis set -- whether it was deleted
    independently or as part of the change under review, there is no content
    left for a lens to open."""
    (repo / "src" / "app.py").unlink()
    files = resolve_files(repo, BoundariesConfig(include=["src/**"]))
    assert Path("src/app.py") not in files
    assert Path("src/generated/schema.py") in files
