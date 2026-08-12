import errno
import os
import subprocess
from pathlib import Path

import pytest

from whetstone.config.model import BoundariesConfig
from whetstone.errors import GitError, ReportError, WriteForbiddenError
from whetstone.report.html import write_report
from whetstone.scope import resolver
from whetstone.scope.resolver import (
    guarded_write,
    is_write_forbidden,
    resolve_files,
)


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


@pytest.mark.parametrize(
    "shape",
    [
        "secrets.env.",
        "secrets.env ",
        "SECRETS.ENV.",
        "secrets.env...",
        "secrets.env. ",
        "secrets.env .",
    ],
    ids=[
        "trailing-dot",
        "trailing-space",
        "case-variant-with-dot",
        "multi-dot",
        "dot-then-space",
        "space-then-dot",
    ],
)
def test_is_write_forbidden_rejects_trailing_dot_or_space_on_a_file(tmp_path, shape):
    """`secrets.env.` is the file `secrets.env` once Windows has stripped it.

    `Path.resolve()` canonicalises only the prefix that already exists on disk,
    so for a file `never_touch` is protecting *before it is created* -- a
    secrets file, a lockfile, a CI workflow -- the barrier matched one spelling
    and the OS wrote another. Case folding does not rescue it either: the folded
    name still carries the decoration.
    """
    boundaries = BoundariesConfig(never_touch=["secrets.env"])
    assert is_write_forbidden(Path(shape), boundaries, project_root=tmp_path)


@pytest.mark.parametrize(
    "shape",
    [
        ".github./workflows/deploy.yml",
        ".github /workflows/deploy.yml",
        ".github/workflows./deploy.yml",
    ],
    ids=["dir-dot", "dir-space", "mid-path-dot"],
)
def test_is_write_forbidden_rejects_a_decorated_directory_component(tmp_path, shape):
    """The decoration does not have to be on the last component to defeat the
    match -- a directory `never_touch` protects need not exist yet either."""
    boundaries = BoundariesConfig(never_touch=[".github/workflows/**"])
    assert is_write_forbidden(Path(shape), boundaries, project_root=tmp_path)


@pytest.mark.parametrize(
    "shape",
    ["secrets.env::$DATA", "secrets.env:hidden", ".github/workflows:x/deploy.yml"],
    ids=["main-stream", "named-stream", "stream-on-a-directory"],
)
def test_is_write_forbidden_rejects_an_alternate_data_stream(tmp_path, shape):
    """Found by inverting the trailing-dot case, and the same defect.

    `:` opens an NTFS alternate data stream. `secrets.env::$DATA` writes
    straight to the main stream of `secrets.env` -- a full overwrite of the
    protected file -- and `secrets.env:hidden` creates that file too. Neither
    spelling is what `resolve()` hands back, so the barrier matched the
    decorated name and missed.
    """
    boundaries = BoundariesConfig(never_touch=["secrets.env", ".github/workflows/**"])
    assert is_write_forbidden(Path(shape), boundaries, project_root=tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="alternate data streams are NTFS-only")
def test_alternate_data_stream_really_writes_the_protected_file(tmp_path):
    """The filesystem proof behind the rule above, not an assertion about it."""
    (tmp_path / "secrets.env::$DATA").write_text("PWNED", encoding="utf-8")
    landed = tmp_path / "secrets.env"
    assert landed.is_file()
    assert landed.read_text(encoding="utf-8") == "PWNED"


def test_is_write_forbidden_rejects_a_dots_only_component(tmp_path):
    """`...` is not `..`; it is a name the filesystem strips to nothing."""
    assert is_write_forbidden(
        Path(".../secrets.env"), BoundariesConfig(), project_root=tmp_path
    )


def test_is_write_forbidden_rejects_decoration_with_never_touch_empty(tmp_path):
    """Same class as a path that escapes the root: the barrier cannot say where
    the write lands, so it refuses -- whatever `never_touch` says."""
    assert is_write_forbidden(
        Path("secrets.env."), BoundariesConfig(), project_root=tmp_path
    )


@pytest.mark.parametrize(
    "shape",
    [
        ".git",
        ".git/config",
        ".git/hooks/pre-commit",
        ".GIT/config",
        ".Git/config",
        "vendor/lib/.git/config",
        "src/../.git/config",
    ],
)
def test_is_write_forbidden_rejects_dot_git_with_never_touch_empty(tmp_path, shape):
    """`.git` is refused whatever the boundaries say, because the one production
    caller passes none.

    `report.write_report` hands `guarded_write` an empty `BoundariesConfig` on
    purpose, so before this rule `--out .git/config` passed the barrier and the
    write truncated the repository's git configuration. A `core.hooksPath` in
    that file runs attacker-chosen code on the next ordinary git command.

    Case variants because Windows and macOS open the same file through them, and
    a nested `.git` because a submodule's git directory is not at the root.
    """
    assert is_write_forbidden(Path(shape), BoundariesConfig(), project_root=tmp_path)


def test_is_write_forbidden_still_allows_a_name_that_merely_starts_with_git(tmp_path):
    """The counterweight. `.gitignore` and `.github/` are ordinary files that a
    report or a fix has every right to be written to."""
    boundaries = BoundariesConfig()
    for allowed in (".gitignore", ".gitattributes", ".github/workflows/ci.yml"):
        assert not is_write_forbidden(
            Path(allowed), boundaries, project_root=tmp_path
        ), allowed


def test_guarded_write_refuses_dot_git_and_leaves_the_file_intact(tmp_path):
    """Through the real write path, and the assertion is the file on disk: the
    barrier returning True is only interesting if nothing was truncated."""
    (tmp_path / ".git").mkdir()
    config = tmp_path / ".git" / "config"
    config.write_text("[core]\n\thooksPath = .githooks\n", encoding="utf-8")
    with pytest.raises(WriteForbiddenError), guarded_write(
        Path(".git/config"), BoundariesConfig(), project_root=tmp_path
    ) as fh:
        fh.write("[core]\n\thooksPath = /tmp/evil\n")
    assert config.read_text(encoding="utf-8") == "[core]\n\thooksPath = .githooks\n"


def test_is_write_forbidden_still_allows_an_undecorated_path(tmp_path):
    """The new rule must not swallow ordinary names. `..` and `.` are legitimate
    components and are not decoration."""
    boundaries = BoundariesConfig(never_touch=["secrets.env"])
    assert not is_write_forbidden(Path("src/app.py"), boundaries, project_root=tmp_path)
    assert not is_write_forbidden(
        Path("src/../src/app.py"), boundaries, project_root=tmp_path
    )
    assert not is_write_forbidden(
        Path("./src/app.py"), boundaries, project_root=tmp_path
    )


def test_is_write_forbidden_control_decorated_path_under_an_existing_dir(tmp_path):
    """The case that already held, kept so the suite proves the new rule rather
    than the old accident: `infra/` exists, so `infra/**` matched the decorated
    name on the strength of the directory glob alone."""
    (tmp_path / "infra").mkdir()
    assert is_write_forbidden(
        Path("infra/new.tf."), _NEVER_TOUCH, project_root=tmp_path
    )


@pytest.mark.skipif(os.name != "nt", reason="only Windows strips trailing dots/spaces")
def test_trailing_dot_really_collapses_on_this_filesystem(tmp_path):
    """Why the rule exists, proved against the filesystem rather than asserted.

    The barrier rule itself is unconditional so the Linux CI leg still catches a
    regression; this test only pins the platform behaviour that motivates it.
    """
    (tmp_path / "secrets.env.").write_text("PWNED", encoding="utf-8")
    landed = tmp_path / "secrets.env"
    assert landed.is_file()
    assert landed.read_text(encoding="utf-8") == "PWNED"


def test_changed_only_survives_diff_relative_in_a_monorepo(monorepo):
    """`diff.relative=true` prints paths relative to the cwd, not the repo root.

    `ls-files --full-name` was pinned to the repo-root frame and `git diff` was
    not, so one documented config emptied the intersection: zero files, no
    error, no skip -- the silent clean scan this module exists to forbid.
    """
    project_root = monorepo / "apps" / "web"
    _git(monorepo, "config", "diff.relative", "true")
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


def test_changed_only_survives_diff_relative_at_the_repo_root(repo):
    """No prefix to strip here, so the config changes nothing -- pinned anyway,
    because `--no-relative` must not break the frame it was already correct in."""
    _git(repo, "config", "diff.relative", "true")
    _git(repo, "checkout", "-b", "feature")
    (repo / "src" / "app.py").write_text("changed", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "change", "--no-gpg-sign")

    files = resolve_files(
        repo, BoundariesConfig(include=["**/*"]), changed_only=True, base_branch="main"
    )
    assert files == (Path("src/app.py"),)


def test_deleted_tracked_files_are_filtered_out(repo):
    """A file removed from the working tree but still in the index has nothing
    to read. It is dropped from the analysis set -- whether it was deleted
    independently or as part of the change under review, there is no content
    left for a lens to open."""
    (repo / "src" / "app.py").unlink()
    files = resolve_files(repo, BoundariesConfig(include=["src/**"]))
    assert Path("src/app.py") not in files
    assert Path("src/generated/schema.py") in files


# --- guarded_write: check and write share one descriptor (issue #11) ----------
#
# `is_write_forbidden` is advisory: it takes a path string, and the path it
# classified is re-resolved by the OS at write time. The PR #4 adversarial gate
# proved the gap -- the barrier returned False for `docs/deploy.yml`, a junction
# was then planted pointing `docs` at `.github/workflows`, and the write landed
# on `.github/workflows/deploy.yml`. `guarded_write` is the closure: it opens
# first and verifies what it is holding.

_WORKFLOW_BARRIER = BoundariesConfig(never_touch=[".github/workflows/**"])


def _plant_junction_on_first_open(monkeypatch, link: Path, target: Path) -> dict:
    """Make the very next `os.open` happen with *link* already pointing at
    *target*, which is the check-then-write window rendered exactly.

    The seam is `os.open` rather than a hook in the production code: a test-only
    branch inside the function under test proves the branch works, not that the
    real path does.
    """
    state = {"planted": False}
    real_open = os.open

    def _spy(path, flags, *args, **kwargs):
        if not state["planted"]:
            state["planted"] = True
            _link_out(link, target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _spy)
    return state


def test_guarded_write_writes_an_ordinary_file(tmp_path):
    with guarded_write(Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path) as fh:
        fh.write("hello")
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "hello"


def test_guarded_write_truncates_an_existing_file(tmp_path):
    (tmp_path / "notes.md").write_text("a much longer previous body", encoding="utf-8")
    with guarded_write(Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path) as fh:
        fh.write("short")
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "short"


def test_guarded_write_refuses_a_never_touch_path(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    with pytest.raises(WriteForbiddenError), guarded_write(
        Path(".github/workflows/deploy.yml"),
        _WORKFLOW_BARRIER,
        project_root=tmp_path,
    ) as fh:
        fh.write("pwned")
    assert not (tmp_path / ".github" / "workflows" / "deploy.yml").exists()


def test_guarded_write_refuses_a_path_outside_the_project(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    with pytest.raises(WriteForbiddenError), guarded_write(
        Path("../escaped.txt"), _WORKFLOW_BARRIER, project_root=project_root
    ) as fh:
        fh.write("pwned")
    assert not (tmp_path / "escaped.txt").exists()


def test_a_junction_planted_between_the_check_and_the_write_is_refused(
    tmp_path, monkeypatch
):
    """The PR #4 proof, reproduced. The barrier passes `docs/deploy.yml`, the
    junction appears, and the open lands inside `.github/workflows`."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    assert not is_write_forbidden(
        Path("docs/deploy.yml"), _WORKFLOW_BARRIER, project_root=tmp_path
    ), "the pre-check must PASS, or this test is not exercising the window"

    state = _plant_junction_on_first_open(
        monkeypatch, tmp_path / "docs", tmp_path / ".github" / "workflows"
    )
    with pytest.raises(WriteForbiddenError), guarded_write(
        Path("docs/deploy.yml"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("pwned")

    assert state["planted"], "the junction was never planted; the window never opened"
    assert not (
        tmp_path / ".github" / "workflows" / "deploy.yml"
    ).exists(), "the refused write left a file behind inside never_touch"


def test_a_junction_already_in_place_is_refused_too(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    _link_out(tmp_path / "docs", tmp_path / ".github" / "workflows")
    with pytest.raises(WriteForbiddenError), guarded_write(
        Path("docs/deploy.yml"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("pwned")
    assert not (tmp_path / ".github" / "workflows" / "deploy.yml").exists()


def test_a_hardlink_is_refused_by_link_count(tmp_path):
    """A hardlink defeats every path-string check: both names are equally
    canonical, `resolve()` has no link to follow, and `is_symlink()` is False.
    Only file identity reveals it, and identity is what a descriptor carries."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    protected = tmp_path / ".github" / "workflows" / "deploy.yml"
    protected.write_text("real workflow", encoding="utf-8")
    alias = tmp_path / "notes.md"
    try:
        os.link(protected, alias)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform
        pytest.skip(f"cannot create a hardlink here: {exc}")

    assert not is_write_forbidden(
        Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path
    ), "no path-string check can see a hardlink; that is the premise of this test"

    with pytest.raises(WriteForbiddenError, match="more than one name"), guarded_write(
        Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("pwned")
    assert protected.read_text(encoding="utf-8") == "real workflow"


def test_a_refused_write_does_not_truncate_the_file_it_refused(tmp_path):
    """Truncation happens through the descriptor AFTER verification. Opening
    with O_TRUNC would destroy the protected file's contents before the barrier
    ever spoke."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    protected = tmp_path / ".github" / "workflows" / "deploy.yml"
    protected.write_text("real workflow", encoding="utf-8")
    with pytest.raises(WriteForbiddenError), guarded_write(
        Path(".github/workflows/deploy.yml"),
        _WORKFLOW_BARRIER,
        project_root=tmp_path,
    ) as fh:
        fh.write("pwned")
    assert protected.read_text(encoding="utf-8") == "real workflow"


def test_guarded_write_refuses_a_character_device(tmp_path):
    """`--out NUL` on Windows writes successfully, discards every byte, and
    leaves nothing on disk. A descriptor knows it is not a regular file."""
    if os.name != "nt":  # pragma: no cover - platform
        device = Path("/dev/null")
        if not device.exists():
            pytest.skip("no character device to point at")
        with pytest.raises(WriteForbiddenError), guarded_write(
            device, _WORKFLOW_BARRIER, project_root=Path("/dev")
        ) as fh:
            fh.write("x")
        return
    with pytest.raises(WriteForbiddenError, match="regular file"), guarded_write(
        Path("NUL"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("x")


def test_guarded_write_refuses_a_directory(tmp_path):
    """A directory fails at `os.open`, on BOTH platforms, and is NOT a barrier
    refusal.

    Measured rather than reasoned about: Linux raises `IsADirectoryError`
    (EISDIR) and Windows raises `PermissionError` (EACCES). Neither reaches the
    `S_ISREG` branch, so a test naming that branch would be asserting a
    mechanism no platform executes -- which is what the previous two versions of
    this test did, silently on Windows and as a hard CI failure on Ubuntu.

    `not isinstance(..., WriteForbiddenError)` is the real assertion. A
    directory is a user naming the wrong thing, not the barrier refusing, and
    the two are told apart by type. See `_reraise_open_failure`.
    """
    (tmp_path / "adir").mkdir()
    with pytest.raises(OSError) as caught, guarded_write(
        Path("adir"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("x")
    assert not isinstance(caught.value, WriteForbiddenError), (
        "a directory is not a barrier refusal; wrapping it re-blurs the line "
        "that keeps ENOSPC and EACCES from reading as security decisions"
    )
    assert caught.value.errno in (errno.EISDIR, errno.EACCES), caught.value


def test_the_directory_error_still_reaches_the_user_named(tmp_path):
    """The counterweight to the decision above: refusing to wrap it here must
    not mean a bare traceback at the command surface. `write_report` owns the
    user-facing wording, and does."""
    target = tmp_path / "report-dir"
    target.mkdir()
    with pytest.raises(ReportError, match="directory"):
        write_report(target, "<html></html>", project_root=tmp_path)


# --- the POSIX open branches, driven on whichever host runs the tests ---------
#
# `_reraise_open_failure` decides between "the barrier refused" and "pass the
# OSError through", and on Windows `_O_NOFOLLOW` is 0 so the symlink arm never
# executes. Reasoning about that arm instead of running it is exactly how the
# directory defect reached CI, so the errno is injected here and both arms run
# on either host. The real POSIX behaviour is separately verified by running the
# suite in a Linux container; this is the part that keeps running afterwards.


@pytest.mark.parametrize("code", [errno.ELOOP, errno.EMLINK])
def test_a_symlink_refusal_is_the_barrier_speaking(monkeypatch, code):
    """Linux reports ELOOP for O_NOFOLLOW on a symlink; the BSDs report EMLINK.
    Measured on Linux: errno 40, ELOOP."""
    monkeypatch.setattr(resolver, "_O_NOFOLLOW", 0x20000)
    with pytest.raises(WriteForbiddenError, match="symlink"):
        resolver._reraise_open_failure(Path("x"), OSError(code, "boom"))


@pytest.mark.parametrize(
    "code",
    [errno.EISDIR, errno.EACCES, errno.ENOSPC, errno.ENAMETOOLONG, errno.ENOENT],
)
def test_every_other_errno_passes_through_with_its_type_and_number(monkeypatch, code):
    monkeypatch.setattr(resolver, "_O_NOFOLLOW", 0x20000)
    with pytest.raises(OSError) as caught:
        resolver._reraise_open_failure(Path("x"), OSError(code, "boom"))
    assert not isinstance(caught.value, WriteForbiddenError)
    assert caught.value.errno == code


def test_without_o_nofollow_an_eloop_is_not_read_as_a_symlink(monkeypatch):
    """On Windows there is no O_NOFOLLOW, so the kernel never refuses FOR that
    reason and an ELOOP arriving anyway means something else. The old spelling
    was `getattr(os, "ELOOP", None)`, which is always None, so every OSError
    carrying `errno=None` took this branch."""
    monkeypatch.setattr(resolver, "_O_NOFOLLOW", 0)
    with pytest.raises(OSError) as caught:
        resolver._reraise_open_failure(Path("x"), OSError(errno.ELOOP, "boom"))
    assert not isinstance(caught.value, WriteForbiddenError)


def test_an_errno_of_none_is_not_read_as_a_symlink(monkeypatch):
    """The regression the named constants fixed, pinned directly."""
    monkeypatch.setattr(resolver, "_O_NOFOLLOW", 0x20000)
    with pytest.raises(OSError) as caught:
        resolver._reraise_open_failure(Path("x"), OSError())
    assert not isinstance(caught.value, WriteForbiddenError)


def test_a_posix_style_directory_failure_propagates_through_guarded_write(
    tmp_path, monkeypatch
):
    """The Linux EISDIR path, driven end-to-end on whichever host runs this.

    Windows raises EACCES for the same input, so without injection the POSIX
    branch would only ever be exercised by CI.
    """
    real_open = os.open

    def _eisdir(path, flags, *args, **kwargs):
        if str(path).endswith("adir"):
            raise IsADirectoryError(errno.EISDIR, "Is a directory")
        return real_open(path, flags, *args, **kwargs)

    (tmp_path / "adir").mkdir()
    monkeypatch.setattr(os, "open", _eisdir)
    with pytest.raises(IsADirectoryError) as caught, guarded_write(
        Path("adir"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("x")
    assert not isinstance(caught.value, WriteForbiddenError)
    assert caught.value.errno == errno.EISDIR


def test_a_created_file_is_removed_when_the_body_raises(tmp_path):
    """`Path.write_text` was one call and could not leave a partial file. Handing
    the caller a descriptor makes that newly possible, so a file this call
    created is removed when the body does not complete."""
    with pytest.raises(RuntimeError), guarded_write(
        Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("partial")
        raise RuntimeError("boom")
    assert not (tmp_path / "notes.md").exists(), "a partial file was left behind"


def _stat_with_a_different_inode(st: os.stat_result) -> os.stat_result:
    """The same file, reported as a different one. Ten fields, in `stat_result`
    order, with `st_ino` bumped -- that plus `st_dev` is what `_identity` reads."""
    return os.stat_result(
        (
            st.st_mode,
            st.st_ino + 1,
            st.st_dev,
            st.st_nlink,
            st.st_uid,
            st.st_gid,
            st.st_size,
            int(st.st_atime),
            int(st.st_mtime),
            int(st.st_ctime),
        )
    )


def test_the_body_failure_cleanup_declines_when_the_name_was_swapped(
    tmp_path, monkeypatch
):
    """The counterweight to the test above, and the second half of a gate that
    only one of the two cleanup paths had.

    `_discard` identity-gates its removal; this path did not, so a redirection
    planted while the caller's body was running turned the tidy-up into a
    deletion of whatever the name pointed at by then. Simulated through `os.stat`
    rather than by really swapping the file, because Windows refuses to replace a
    file that still has an open handle, which is exactly the window under test.
    """
    real_stat = os.stat
    planted: list[bool] = []

    def _swapped(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if planted and str(path).endswith("notes.md"):
            return _stat_with_a_different_inode(st)
        return st

    monkeypatch.setattr(os, "stat", _swapped)
    with pytest.raises(RuntimeError), guarded_write(
        Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("partial")
        # The redirection appears HERE, after `_verify` has already passed.
        # Planting it earlier is a different defect, and one the identity check
        # inside `_verify` already refuses.
        planted.append(True)
        raise RuntimeError("boom")
    assert (tmp_path / "notes.md").exists(), (
        "the cleanup deleted a file it could not show was the one it created"
    )


def test_reraise_open_failure_is_annotated_as_never_returning():
    """Mechanical, because the consequence is silent.

    `_open_for_write` is declared `-> tuple[int, bool]` and ends both of its
    `except OSError` arms with this call. If it is ever typed as returning, those
    arms fall off the end as `None` and `guarded_write` unpacks that into a
    TypeError instead of raising the refusal.
    """
    import typing

    hints = typing.get_type_hints(resolver._reraise_open_failure)
    assert hints["return"] is typing.NoReturn, hints


def test_an_existing_file_is_left_truncated_when_the_body_raises(tmp_path):
    """The honest other half. A file that already existed was truncated at step
    6 by design and its previous contents cannot be brought back -- `write_text`
    had exactly the same property, so nothing is lost that was ever guaranteed.
    It is NOT deleted, because it is not ours to delete.

    Closure is asserted on the HANDLE, not only through `unlink`. Unlinking a
    file with a live descriptor fails on Windows and succeeds on POSIX, so the
    `unlink` alone detects a leak on one leg and passes vacuously on the leg CI
    runs most often -- it would have stayed green with the cleanup deleted.
    `fh.closed` holds on both, and the `unlink` stays as the Windows-specific
    probe for a lock the handle flag cannot see.
    """
    target = tmp_path / "notes.md"
    target.write_text("the previous body", encoding="utf-8")
    handle = None
    with pytest.raises(RuntimeError), guarded_write(
        Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        handle = fh
        fh.write("partial")
        raise RuntimeError("boom")
    assert handle is not None, "the context manager never yielded"
    assert handle.closed, "the descriptor outlived the context manager"
    assert target.exists()
    target.unlink()


def test_a_failure_between_the_open_and_the_handle_removes_the_created_file(
    tmp_path, monkeypatch
):
    """ENOSPC on the truncate, or anything else after the open. Cleanup used to
    be wired only into the barrier's refusal path, so this left the file."""
    real_truncate = os.truncate

    def _boom(fd, length):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "truncate", _boom)
    with pytest.raises(OSError), guarded_write(
        Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("x")
    monkeypatch.setattr(os, "truncate", real_truncate)
    assert not (tmp_path / "notes.md").exists()


def test_the_descriptor_is_closed_exactly_once(tmp_path, monkeypatch):
    """Two `os.close` calls on one number are harmless single-threaded and a
    live hazard once M1a's provider makes threads plausible: the second can land
    on a descriptor another thread has since been handed."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    protected = tmp_path / ".github" / "workflows" / "deploy.yml"
    protected.write_text("real workflow", encoding="utf-8")
    try:
        os.link(protected, tmp_path / "notes.md")
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform
        pytest.skip(f"cannot create a hardlink here: {exc}")

    closed: list[int] = []
    real_close = os.close

    def _record(fd):
        closed.append(fd)
        return real_close(fd)

    # The hardlink refusal, not a never_touch one: the never_touch pre-check
    # fires BEFORE any open, so no descriptor would exist to close and this test
    # would pass by measuring nothing.
    monkeypatch.setattr(os, "close", _record)
    with pytest.raises(WriteForbiddenError), guarded_write(
        Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("pwned")
    assert closed, "no descriptor was closed at all; the test is measuring nothing"
    assert len(closed) == len(set(closed)), f"a descriptor was closed twice: {closed}"


def test_the_descriptor_is_closed_exactly_once_without_needing_a_hardlink(
    tmp_path, monkeypatch
):
    """The same property as the test above, on a path no filesystem feature can
    take away.

    That one skips when the temporary directory cannot make a hardlink, and it
    was the only test asserting close-once -- so on such a host the property
    shipped unverified and the run still reported clean. A failure anywhere
    after the open reaches `_discard` just as a barrier refusal does, and
    `os.truncate` is the first thing that runs there, so forcing it is enough.
    """
    closed: list[int] = []
    real_close = os.close

    def _record(fd):
        closed.append(fd)
        return real_close(fd)

    def _no_space(fd, length):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "close", _record)
    monkeypatch.setattr(os, "truncate", _no_space)
    with pytest.raises(OSError), guarded_write(
        Path("notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("never reached")
    assert closed, "no descriptor was closed at all; the test is measuring nothing"
    assert len(closed) == len(set(closed)), f"a descriptor was closed twice: {closed}"


def test_an_unrelated_os_error_is_not_reported_as_a_barrier_refusal(tmp_path):
    """ENOENT from a missing parent directory is not the barrier saying no.
    Wrapping every OSError as WriteForbiddenError with `from None` made ENOSPC,
    EACCES and ENAMETOOLONG all read as "you are not allowed" and discarded the
    errno that said otherwise."""
    with pytest.raises(OSError) as caught, guarded_write(
        Path("no-such-dir/notes.md"), _WORKFLOW_BARRIER, project_root=tmp_path
    ) as fh:
        fh.write("x")
    assert not isinstance(caught.value, WriteForbiddenError)
    assert caught.value.errno is not None


def test_the_barrier_scan_is_not_vacuous(tmp_path):
    """Every refusal test above asserts an exception. This one asserts the
    permitted population is non-empty: a `guarded_write` that refused
    everything would satisfy all of them and be useless."""
    permitted = [Path("notes.md"), Path("src/app.py"), Path("docs/readme.md")]
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    assert permitted
    for target in permitted:
        with guarded_write(target, _WORKFLOW_BARRIER, project_root=tmp_path) as fh:
            fh.write("ok")
        assert (tmp_path / target).read_text(encoding="utf-8") == "ok"
