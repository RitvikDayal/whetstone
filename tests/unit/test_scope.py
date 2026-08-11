import os
import subprocess
from pathlib import Path

import pytest

from whetstone.config.model import BoundariesConfig
from whetstone.errors import GitError
from whetstone.scope import resolver
from whetstone.scope.resolver import is_write_forbidden, resolve_files


def _git(repo: Path, *args: str) -> None:
    # utf-8, not the locale codec: these fixtures commit non-ASCII filenames and
    # git echoes them back, which blows up a cp1252 decode in the test harness
    # itself before the code under test is ever reached.
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="surrogateescape",
    )


def _init(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init(tmp_path)
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


def test_is_write_forbidden_uses_never_touch(tmp_path):
    boundaries = BoundariesConfig(never_touch=["infra/**", ".github/workflows/**"])
    assert is_write_forbidden(Path("infra/main.tf"), boundaries, project_root=tmp_path)
    assert is_write_forbidden(
        Path(".github/workflows/ci.yml"), boundaries, project_root=tmp_path
    )
    assert not is_write_forbidden(Path("src/app.py"), boundaries, project_root=tmp_path)


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


@pytest.fixture
def monorepo(tmp_path: Path) -> Path:
    """A repo whose Whetstone project root is `apps/web`, not the repo root.

    `find_config` supports a whetstone.yaml at or below the worktree root, so
    this layout is a supported one, not an exotic edge case.
    """
    _init(tmp_path)
    for rel in ["apps/web/src/a.py", "apps/web/infra/main.tf", "tools/build.py"]:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init", "--no-gpg-sign")
    return tmp_path


def test_project_root_below_the_git_root_sees_only_its_own_files(monorepo):
    """Returned paths are relative to the PROJECT root, not the repo root."""
    files = resolve_files(monorepo / "apps" / "web", BoundariesConfig(include=["**/*"]))
    assert files == (Path("infra/main.tf"), Path("src/a.py"))


def test_changed_only_finds_the_change_when_project_root_is_below_the_git_root(
    monorepo,
):
    """`ls-files` prints cwd-relative, `diff --name-only` prints repo-relative.

    Intersecting the two raw made a monorepo project resolve to zero files: a
    run that examined nothing, raised nothing, and reported clean.
    """
    project_root = monorepo / "apps" / "web"
    _git(monorepo, "checkout", "-b", "feature")
    (project_root / "src" / "a.py").write_text("changed", encoding="utf-8")
    _git(monorepo, "add", "apps/web/src/a.py")
    _git(monorepo, "commit", "-m", "change", "--no-gpg-sign")

    files = resolve_files(
        project_root,
        BoundariesConfig(include=["**/*"]),
        changed_only=True,
        base_branch="main",
    )
    assert files == (Path("src/a.py"),)


def test_changed_only_does_not_match_the_same_relative_path_elsewhere(monorepo):
    """The intersection has to happen in repo-root terms, not by bare name.

    `src/a.py` exists at the repo root and in a sibling project too. Comparing
    project-relative names against a repo-relative diff would report this run's
    `src/a.py` as changed because somebody else's was.
    """
    project_root = monorepo / "apps" / "web"
    for rel in ["src/a.py", "apps/web-admin/src/a.py"]:
        target = monorepo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    _git(monorepo, "add", ".")
    _git(monorepo, "commit", "-m", "add lookalikes", "--no-gpg-sign")

    _git(monorepo, "checkout", "-b", "feature")
    (monorepo / "src" / "a.py").write_text("changed", encoding="utf-8")
    (monorepo / "apps" / "web-admin" / "src" / "a.py").write_text(
        "changed", encoding="utf-8"
    )
    _git(monorepo, "add", ".")
    _git(monorepo, "commit", "-m", "change the lookalikes", "--no-gpg-sign")

    files = resolve_files(
        project_root,
        BoundariesConfig(include=["**/*"]),
        changed_only=True,
        base_branch="main",
    )
    assert files == ()


def test_changed_only_ignores_changes_outside_the_project_root(monorepo):
    project_root = monorepo / "apps" / "web"
    _git(monorepo, "checkout", "-b", "feature")
    (monorepo / "tools" / "build.py").write_text("changed", encoding="utf-8")
    _git(monorepo, "add", "tools/build.py")
    _git(monorepo, "commit", "-m", "change", "--no-gpg-sign")

    files = resolve_files(
        project_root,
        BoundariesConfig(include=["**/*"]),
        changed_only=True,
        base_branch="main",
    )
    assert files == ()


@pytest.mark.parametrize(
    # Escapes, not literals: both spell 'cafe.py' with an acute accent, one
    # precomposed (U+00E9) and one decomposed (e + U+0301). Written literally an
    # editor would normalise one into the other and the pair would stop testing
    # anything.
    'name',
    ['café.py', 'café.py'],
    ids=['nfc', 'nfd'],
)
def test_non_ascii_filenames_survive_git_decoding(tmp_path, name):
    """git emits path bytes as stored (UTF-8). Decoding them with the locale
    codec mangled the composed form into a path that does not exist (silently
    dropped) and crashed outright on the decomposed one."""
    _init(tmp_path)
    try:
        (tmp_path / name).write_text("x", encoding="utf-8")
    except (OSError, UnicodeError) as exc:  # pragma: no cover - filesystem dependent
        pytest.skip(f"cannot create {ascii(name)} on this filesystem: {exc}")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init", "--no-gpg-sign")

    files = resolve_files(tmp_path, BoundariesConfig(include=["**/*"]))
    assert files == (Path(name),)


def test_undecodable_path_bytes_are_reported_not_dropped(repo, monkeypatch):
    """A name whose bytes are not UTF-8 -- created on a latin-1 Linux box, say.

    surrogateescape keeps the run alive past the decode, which is the point;
    the file still cannot be opened, so the run must say so rather than quietly
    examining one file fewer. Faked because such a name cannot be created on
    every platform this suite runs on.
    """
    real = resolver._git

    def fake(project_root: Path, args: list[str]) -> str:
        if args[0] == "ls-files":
            return "src/app.py\0src/bad\udce9.py\0"
        return real(project_root, args)

    monkeypatch.setattr(resolver, "_git", fake)
    with pytest.raises(GitError, match="not valid UTF-8"):
        resolve_files(repo, BoundariesConfig(include=["src/**"]))


def test_undecodable_path_outside_the_boundaries_is_not_an_error(repo, monkeypatch):
    """Only files the run claimed to examine matter. One it was never going to
    open is not an unreported gap, and failing the run over it would leave no
    way out."""
    real = resolver._git

    def fake(project_root: Path, args: list[str]) -> str:
        if args[0] == "ls-files":
            return "src/app.py\0docs/bad\udce9.md\0"
        return real(project_root, args)

    monkeypatch.setattr(resolver, "_git", fake)
    assert resolve_files(repo, BoundariesConfig(include=["src/**"])) == (
        Path("src/app.py"),
    )


_NEVER_TOUCH = BoundariesConfig(never_touch=["infra/**"])


def test_is_write_forbidden_rejects_an_absolute_path_inside_the_root(tmp_path):
    """`project_root / rel` is what a caller writes; it must still be caught."""
    assert is_write_forbidden(
        tmp_path / "infra" / "main.tf", _NEVER_TOUCH, project_root=tmp_path
    )


def test_is_write_forbidden_rejects_a_traversal_into_a_protected_path(tmp_path):
    assert is_write_forbidden(
        Path("src/../infra/main.tf"), _NEVER_TOUCH, project_root=tmp_path
    )


@pytest.mark.parametrize(
    "shape",
    ["/infra/main.tf", "../elsewhere/main.tf", "//server/share/main.tf"],
    ids=["rooted-elsewhere", "escapes-upward", "unc-or-double-slash"],
)
def test_is_write_forbidden_rejects_paths_outside_the_project_root(tmp_path, shape):
    """A barrier that cannot place a path must refuse it, not allow it. Holds
    with never_touch empty too: writes never leave the worktree."""
    assert is_write_forbidden(Path(shape), _NEVER_TOUCH, project_root=tmp_path)
    assert is_write_forbidden(Path(shape), BoundariesConfig(), project_root=tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="drive-relative paths are Windows-only")
def test_is_write_forbidden_rejects_a_drive_relative_path(tmp_path):
    """`C:infra` means "infra under the current directory of drive C:", which is
    not knowable from here."""
    assert is_write_forbidden(
        Path("C:infra/main.tf"), _NEVER_TOUCH, project_root=tmp_path
    )
    assert is_write_forbidden(Path("C:src/app.py"), _NEVER_TOUCH, project_root=tmp_path)


def _link_out(link: Path, target: Path) -> None:
    """Point *link* at *target*, by whatever means this host allows.

    A plain symlink needs a privilege an unelevated Windows user does not have,
    and skipping there would leave the write barrier unverified on the platform
    it is most likely to be defeated on. A junction needs no privilege and is
    followed by `Path.resolve()` just the same.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform
        if os.name != "nt":
            pytest.skip(f"cannot create a symlink here: {exc}")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover - platform
        pytest.skip(f"neither a symlink nor a junction could be created: {result}")


def test_is_write_forbidden_rejects_a_symlink_out_of_the_project_root(tmp_path):
    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    (project_root / "src").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    _link_out(project_root / "escape", outside)
    assert is_write_forbidden(
        Path("escape/secret.txt"), _NEVER_TOUCH, project_root=project_root
    )


def test_is_write_forbidden_matches_a_case_variant_of_never_touch(tmp_path):
    """gitwildmatch is case-sensitive; Windows and macOS are not. `INFRA/` and
    `infra/` are the same directory there, so the barrier folds case."""
    assert is_write_forbidden(
        Path("INFRA/main.tf"), _NEVER_TOUCH, project_root=tmp_path
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
