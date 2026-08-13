"""The check that does not rely on the CLI honouring a flag.

Every test here mutates a real directory and asks the sentinel whether it
noticed. None of them assert on how it looked -- the argv-shaped assertion is
exactly what let the permission defect ship green.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from whetstone.provider import sentinel as sentinel_module
from whetstone.provider.sentinel import assert_unchanged, fingerprint

# NOT a module-level skip.
#
# `pytestmark = skipif(...)` applied to every test here, so a leg without `git`
# on PATH would delete the entire sentinel suite and report success -- including
# the regression test for a proven code-execution defect. A skipped test is not
# a passing test, and a skip that can silently become permanent on one leg of a
# four-leg matrix is the same shape as a check that quietly never runs.
#
# It was also far wider than its reason: every walk-mode test here needs no git
# at all. Those now run everywhere, and `needs_git` marks only the tests that
# genuinely spawn the binary.
_HAVE_GIT = shutil.which("git") is not None

needs_git = pytest.mark.skipif(not _HAVE_GIT, reason="git is not on PATH")


def test_git_is_available_on_this_leg():
    """The skip above is load-bearing, so its premise gets its own assertion.

    If this fails, every `needs_git` test in this file is being skipped and the
    sentinel's git mode is unverified on this leg. That is a CI configuration
    defect, and it must be visible as a failure rather than as a green run with
    a smaller test count.
    """
    assert _HAVE_GIT, (
        "git is not on PATH, so every git-mode sentinel test is skipped -- "
        "including the regression test for the core.fsmonitor code execution "
        "defect. Install git on this leg."
    )


def _git(root, *args):
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        # The same sanitised environment the sentinel uses, so the fixture
        # cannot be steered by an ambient GIT_* variable or by the developer's
        # ~/.gitconfig. Without it these tests inherit machine-level settings
        # and their premises change per machine.
        env=sentinel_module._git_env(),
    )


@pytest.fixture
def repo(tmp_path):
    """A real repository with one commit. `git init` alone leaves an unborn
    HEAD, which is a different state and is covered separately."""
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "README.md").write_text("start\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "--quiet", "-m", "first", "--no-gpg-sign")
    return tmp_path


@needs_git
def test_a_quiet_stage_reports_nothing(repo):
    before = fingerprint(repo)
    assert assert_unchanged(repo, before) is None


@needs_git
def test_a_created_file_is_caught(repo):
    """The reviewer's stage created `pwned.txt` and `permission_denials` was
    empty, because a tool that is simply absent is never refused. This is the
    check that sees it anyway."""
    before = fingerprint(repo)
    (repo / "pwned.txt").write_text("owned\n", encoding="utf-8")
    mutation = assert_unchanged(repo, before)
    assert mutation is not None
    assert "pwned.txt" in mutation


@needs_git
def test_an_appended_file_is_caught(repo):
    """The reviewer also appended to README.md, which leaves the file list
    identical. A sentinel that only counted paths would miss it."""
    before = fingerprint(repo)
    with (repo / "README.md").open("a", encoding="utf-8") as handle:
        handle.write("appended\n")
    mutation = assert_unchanged(repo, before)
    assert mutation is not None
    assert "README.md" in mutation


@needs_git
def test_a_deleted_file_is_caught(repo):
    before = fingerprint(repo)
    (repo / "README.md").unlink()
    assert assert_unchanged(repo, before) is not None


# --- the seven classes the status-letters-only version missed -------------------
#
# Each of these was PROVEN missed by an adversarial reviewer against the first
# sentinel, which recorded `git status --porcelain` output and nothing else.
# Row one is the original attack verbatim.


@needs_git
def test_appending_to_an_ALREADY_DIRTY_tracked_file_is_caught(repo):
    """THE ONE THAT MATTERED MOST.

    The attack this whole module was written against was a stage appending to
    `README.md`. If the file is already modified when the stage starts -- which
    is the normal state of a working tree mid-run -- `git status` says
    ` M README.md` before AND after, and the first sentinel reported a clean
    worktree and `ok=True`.
    """
    (repo / "README.md").write_text("start\ndirty already\n", encoding="utf-8")
    before = fingerprint(repo)
    assert " M README.md" in before.replace("\t", " ") or "M README.md" in before

    with (repo / "README.md").open("a", encoding="utf-8") as handle:
        handle.write("appended by the stage\n")

    mutation = assert_unchanged(repo, before)
    assert mutation is not None, "a dirty file getting dirtier is still a mutation"
    assert "README.md" in mutation


@needs_git
def test_rewriting_an_ALREADY_UNTRACKED_file_is_caught(repo):
    """`?? notes.txt` before and after. Same shape as the row above."""
    (repo / "notes.txt").write_text("original\n", encoding="utf-8")
    before = fingerprint(repo)
    (repo / "notes.txt").write_text("replaced by the stage\n", encoding="utf-8")
    assert assert_unchanged(repo, before) is not None


@needs_git
def test_rewriting_a_STAGED_file_is_caught(repo):
    """`M  src.py` before and after."""
    (repo / "src.py").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "src.py")
    before = fingerprint(repo)
    (repo / "src.py").write_text("replaced by the stage\n", encoding="utf-8")
    assert assert_unchanged(repo, before) is not None


@needs_git
def test_writing_a_git_hook_is_caught(repo):
    """`git status` never reports anything under `.git/`, so this was wholly
    invisible -- while `.git/hooks/` is simultaneously how a repository becomes
    a program that runs on the next checkout."""
    before = fingerprint(repo)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-checkout").write_text("#!/bin/sh\necho owned\n", encoding="utf-8")
    mutation = assert_unchanged(repo, before)
    assert mutation is not None
    assert "hooks" in mutation


@needs_git
def test_editing_git_config_is_caught(repo):
    """The same blind spot, and the more dangerous half: `.git/config` is where
    `core.fsmonitor` lives, which is a program git executes."""
    before = fingerprint(repo)
    _git(repo, "config", "whetstone.plantedbythestage", "yes")
    assert assert_unchanged(repo, before) is not None


@needs_git
def test_a_write_hidden_by_a_gitignore_of_everything_is_caught(repo):
    """An attacker-supplied `.gitignore` of `*` hides every new file from
    `git status`. `--ignored` is what closes it, and it only became affordable
    once no stage held a shell -- nothing churns a build directory now."""
    (repo / ".gitignore").write_text("*\n", encoding="utf-8")
    _git(repo, "add", "-f", ".gitignore")
    _git(repo, "commit", "--quiet", "-m", "ignore all", "--no-gpg-sign")
    before = fingerprint(repo)

    (repo / "pwned.txt").write_text("owned\n", encoding="utf-8")
    plain = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "pwned.txt" not in plain, (
        "the premise: an unignored status must NOT show this file, or the test "
        "proves nothing about --ignored"
    )

    mutation = assert_unchanged(repo, before)
    assert mutation is not None
    assert "pwned.txt" in mutation


@needs_git
def test_a_write_then_delete_is_the_one_that_cannot_be_caught(repo):
    """Documented rather than fixed, because no before/after comparison can see
    it. The disclosure list says so; this pins that the claim is honest and not
    an unnoticed gap."""
    before = fingerprint(repo)
    scratch = repo / "transient.txt"
    scratch.write_text("here and gone\n", encoding="utf-8")
    scratch.unlink()
    assert assert_unchanged(repo, before) is None


@needs_git
def test_the_sentinel_is_scoped_to_root_not_to_the_whole_repository(repo):
    """`git status` reports the WHOLE repository regardless of cwd, so watching
    a subdirectory reported edits made anywhere else in the user's repo -- and a
    stage would fail as a read-only violation because an editor saved a file two
    directories over. The `-- .` pathspec is what scopes it."""
    watched = repo / "sub"
    watched.mkdir()
    (watched / "inside.txt").write_text("a\n", encoding="utf-8")
    before = fingerprint(watched)

    (repo / "outside.txt").write_text("touched by someone else\n", encoding="utf-8")
    assert assert_unchanged(watched, before) is None, (
        "an edit outside the watched root is not this stage's mutation"
    )

    (watched / "inside.txt").write_text("b\n", encoding="utf-8")
    assert assert_unchanged(watched, before) is not None


@needs_git
def test_git_does_not_execute_a_program_named_by_the_inspected_repository(repo):
    """`core.fsmonitor` names a program git RUNS, and git reads it from the
    worktree's own `.git/config`. A reviewer planted one and got code execution
    out of `fingerprint()` -- before any model ran, with zero tokens spent, on a
    tool whose entire purpose is inspecting other people's repositories.

    THE PAYLOAD IS A SHELL SCRIPT ON BOTH PLATFORMS. The first version of this
    test used a `.bat` on Windows; git runs hooks through its own `sh`, could
    not execute it, and the test passed against a completely unhardened
    sentinel. The control below is what makes that impossible to repeat -- it
    fires the payload through plain `git` first, so a case that proves nothing
    fails loudly instead of passing quietly. Git for Windows ships `sh`.
    """
    marker = repo / "RCE-PROOF.txt"
    payload = repo / "evil.sh"
    payload.write_text(
        f'#!/bin/sh\necho owned > "{marker.as_posix()}"\nexit 1\n', encoding="utf-8"
    )
    payload.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", payload.as_posix())

    # THE CONTROL: unhardened git must execute it, or this test is vacuous.
    subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, stdin=subprocess.DEVNULL,
    )
    assert marker.exists(), (
        "plain `git status` did not run the planted fsmonitor program, so this "
        "test cannot show that the hardening is what stops it"
    )
    marker.unlink()

    printed = fingerprint(repo)

    assert not marker.exists(), (
        "the inspected repository executed its own core.fsmonitor program "
        "through the sentinel"
    )
    # AND the sentinel was still using git. Without this, a `-c` override that
    # made `git status` exit non-zero would degrade to `mode git-unavailable`,
    # no fsmonitor program would run for that reason, the marker would be
    # absent, and this test would pass against a sentinel that had silently
    # stopped using git inside a real repository.
    assert printed.startswith("mode git\n"), printed.splitlines()[0]


@needs_git
def test_a_new_commit_is_caught(repo):
    """A stage that stages and commits its own changes leaves a CLEAN worktree.
    `git status` alone would report nothing; HEAD is in the fingerprint for
    exactly this."""
    before = fingerprint(repo)
    (repo / "sneaky.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "sneaky.txt")
    _git(repo, "commit", "--quiet", "-m", "covered my tracks", "--no-gpg-sign")

    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert porcelain == "", "the premise: committing leaves status silent"

    mutation = assert_unchanged(repo, before)
    assert mutation is not None, "a commit hides the change from status alone"
    # The `head ` PREFIX on a line, not the substring "head" anywhere.
    # Committing also moves `refs/heads/<branch>`, and `_git_surface` emits
    # `ref refs/heads/master <sha>` -- which contains "head". So the substring
    # form passed even with the head line deleted from the fingerprint, which
    # is the one thing this test exists to hold.
    assert any(
        part.strip().startswith("head ")
        for chunk in mutation.split("; ")
        for part in chunk.split(": ")
    ), mutation


@needs_git
def test_git_init_on_a_bare_directory_is_caught(tmp_path):
    """`git init` was one of the three things the reviewer's stage did. Before
    it, the root is not a repository at all, so the fingerprint mode itself
    changes -- and the mode is in the fingerprint for that reason."""
    before = fingerprint(tmp_path)
    assert before.startswith("mode git-unavailable"), (
        "the premise is that pytest's tmp_path is not inside a repository -- "
        "git walks UP looking for .git, so a tmp root nested under one would "
        f"answer here. It answered: {before.splitlines()[0]}"
    )
    _git(tmp_path, "init", "--quiet")
    assert assert_unchanged(tmp_path, before) is not None


def test_a_directory_that_is_not_a_repository_is_still_watched(tmp_path):
    """Degrading to a walk is a degradation, not an exemption. A sentinel that
    quietly returned None outside a repository would be off in exactly the
    scratch directories a stage is most likely to be pointed at."""
    (tmp_path / "existing.txt").write_text("a\n", encoding="utf-8")
    before = fingerprint(tmp_path)
    assert assert_unchanged(tmp_path, before) is None

    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "new.txt").write_text("b\n", encoding="utf-8")
    mutation = assert_unchanged(tmp_path, before)
    assert mutation is not None
    assert "new.txt" in mutation


def test_the_walk_notices_a_change_of_length(tmp_path):
    """Size is in the walk entry. Asserting only on the name would pass against
    a fingerprint that listed paths and nothing else."""
    target = tmp_path / "f.txt"
    target.write_text("aaaa\n", encoding="utf-8")
    before = fingerprint(tmp_path)
    target.write_text("bbbbbbbbbb\n", encoding="utf-8")
    assert assert_unchanged(tmp_path, before) is not None


def test_the_walk_notices_a_same_length_rewrite(tmp_path):
    """The docstring claimed mtime covers this and nothing tested it, so
    dropping `st_mtime_ns` from the walk entry survived -- the length-change
    case above carried the whole claim on `st_size` alone.

    `time.sleep` rather than a mock: `st_mtime_ns` resolution is
    filesystem-dependent (NTFS ~100ns, ext4 1ns, but HFS+ and some CI overlays
    are coarser), and a rewrite inside one tick is genuinely indistinguishable.
    """
    # write_bytes, not write_text: on Windows text mode turns "\n" into "\r\n",
    # so the two writes are 5 and 6 bytes and the size field catches it -- which
    # would make this test pass without mtime ever being consulted. The premise
    # assertion below caught exactly that.
    target = tmp_path / "f.txt"
    target.write_bytes(b"aaaa\n")
    before = fingerprint(tmp_path)

    # WAIT UNTIL THE CLOCK OBSERVABLY MOVES, bounded. A fixed 0.05s sleep is a
    # guess about filesystem timestamp granularity: FAT32 gives 2s and HFS+
    # gives 1s, so on such a leg both writes land in one tick, the fingerprint
    # is identical, and the test fails for a reason that is not a defect in
    # sentinel.py. Skipping instead would remove the only test holding
    # st_mtime_ns in the walk entry, so it adapts rather than opts out.
    stamp = target.stat().st_mtime_ns
    deadline = time.monotonic() + 5.0
    while True:
        target.write_bytes(b"bbbb\n")
        if target.stat().st_mtime_ns != stamp:
            break
        if time.monotonic() > deadline:
            pytest.fail(
                "this filesystem did not advance st_mtime_ns within 5s, so a "
                "same-length rewrite is genuinely indistinguishable here"
            )
        time.sleep(0.05)

    assert target.stat().st_size == 5, "the premise: identical length"
    assert assert_unchanged(tmp_path, before) is not None


def test_the_walk_notices_a_length_change_the_clock_cannot_show(tmp_path):
    """`st_size` was droppable: both other walk cases also move `st_mtime_ns`,
    so mtime alone carried them and size proved nothing.

    `os.utime` restores the original timestamp, which is the only way to isolate
    size -- and it is not a contrivance. A coarse-granularity filesystem, a
    restored backup, an archive extraction and a clock step all produce a
    changed file whose mtime does not distinguish it.
    """
    target = tmp_path / "f.txt"
    target.write_bytes(b"aaaa\n")
    stamp = target.stat().st_mtime_ns
    before = fingerprint(tmp_path)

    target.write_bytes(b"aaaa-and-then-some\n")
    os.utime(target, ns=(stamp, stamp))
    assert target.stat().st_mtime_ns == stamp, "the premise: mtime is unchanged"

    assert assert_unchanged(tmp_path, before) is not None


def test_a_deletion_is_described_as_a_removal(tmp_path):
    """In git mode a deletion ADDS a status line, so the git tests exercise only
    the `added` branch. In walk mode -- which every provider test uses -- a
    deletion is removal-only, and blanking the `removed` block left the sentence
    with no detail at all while `assert_unchanged` still returned non-None."""
    (tmp_path / "gone.txt").write_text("x\n", encoding="utf-8")
    before = fingerprint(tmp_path)
    (tmp_path / "gone.txt").unlink()
    mutation = assert_unchanged(tmp_path, before)
    assert mutation is not None
    assert "was:" in mutation
    assert "gone.txt" in mutation


def test_several_new_files_are_all_named_up_to_the_cap(tmp_path):
    """Every other test changes exactly one file, so truncating the report to
    `added[:1]` survived."""
    before = fingerprint(tmp_path)
    for index in range(3):
        (tmp_path / f"new{index}.txt").write_text("x\n", encoding="utf-8")
    mutation = assert_unchanged(tmp_path, before)
    assert mutation is not None
    for index in range(3):
        assert f"new{index}.txt" in mutation


def test_the_walk_is_ordered_and_uses_posix_separators(tmp_path):
    """`dirs.sort()` and `sorted(names)` are load-bearing on Linux and
    accidentally redundant on Windows: NTFS returns directory entries in name
    order, ext4 returns hash order. Combined with the 20,000-entry cap, an
    unsorted walk changes WHICH entries land in the fingerprint when a file is
    added -- a large spurious mutation report, on the Ubuntu legs only.
    """
    for name in ("zeta.txt", "alpha.txt", "mid.txt"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    (nested / "leaf.txt").write_text("x\n", encoding="utf-8")

    entries = [
        line
        for line in fingerprint(tmp_path).splitlines()
        if not line.startswith("mode")
    ]
    assert entries == sorted(entries), f"the walk is unordered: {entries}"
    assert any("sub/deeper/leaf.txt" in line for line in entries), (
        f"paths must be posix-normalised so a fingerprint reads the same on "
        f"both legs: {entries}"
    )
    assert not any("\\" in line for line in entries), entries


def test_a_file_added_past_the_walk_cap_is_still_visible(tmp_path, monkeypatch):
    """Truncation used to be silent: with the cap reached, everything beyond it
    was simply absent, so adding a file there changed nothing. The total count
    is in the fingerprint so the cap bounds the REPORT, never the detection."""
    monkeypatch.setattr(sentinel_module, "_WALK_CAP", 3)
    for index in range(5):
        (tmp_path / f"f{index}.txt").write_bytes(b"x")
    before = fingerprint(tmp_path)
    assert "5 entries" in before, before

    (tmp_path / "zzz-past-the-cap.txt").write_bytes(b"x")
    mutation = assert_unchanged(tmp_path, before)
    assert mutation is not None, "a write past the cap must not be invisible"


def test_a_hanging_git_degrades_to_the_walk_and_says_so(tmp_path, monkeypatch):
    """THE EFFECT, not the kwarg.

    The previous version recorded `kwargs.get("timeout")` and asserted it was
    not None. That is the argv-shaped assertion this file's own docstring bans:
    it passes against `timeout=1e-9`, and against a `_git` that caught
    `TimeoutExpired` and returned a fabricated status string.

    What has to hold is that a git which never answers cannot hang the stage,
    and that the degradation reaches the fingerprint with its reason -- because
    a sentinel that silently switched modes is a sentinel reporting on
    something other than what the reader thinks.
    """
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")

    def always_times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(sentinel_module.subprocess, "run", always_times_out)

    printed = fingerprint(tmp_path)

    assert printed.startswith("mode git-unavailable"), printed.splitlines()[0]
    assert "did not answer" in printed.splitlines()[0], (
        "the reader is told the mode degraded and must also be told why"
    )
    assert any("a.txt" in line for line in printed.splitlines()), (
        "degrading to the walk must still watch the directory"
    )


@needs_git
def test_a_head_that_could_not_be_READ_is_not_reported_as_unborn(repo, monkeypatch):
    """`head unborn` is a claim about the world: this repository has no commit
    yet. A `rev-parse HEAD` that TIMED OUT supports no such claim, and writing
    it as `unborn` turns a failure to look into a statement about what was seen.

    The two are distinguished by HOW git failed -- a non-zero exit inside a
    repository git has just answered about means no commit, a timeout means
    nothing. Without this test, collapsing them was invisible: the unborn test
    passes either way, because in a genuinely unborn repo both branches produce
    the same line.
    """
    real_run = sentinel_module.subprocess.run

    def timeout_only_head(args, **kwargs):
        if "rev-parse" in args and "HEAD" in args:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)
        return real_run(args, **kwargs)

    monkeypatch.setattr(sentinel_module.subprocess, "run", timeout_only_head)

    printed = fingerprint(repo)

    assert "head unborn" not in printed, (
        "a HEAD that could not be read was recorded as a repository with no "
        "commits, which is a different fact about the world"
    )
    assert "head unreadable" in printed
    assert "did not answer" in printed


@needs_git
def test_a_failing_git_names_its_reason_rather_than_just_the_mode(
    tmp_path, monkeypatch
):
    """`mode git-unavailable` read identically for a missing binary, a timeout,
    a stale index.lock and a permission error. The one line that exists to
    prevent a silent partial answer stopped one step short of saying anything."""
    monkeypatch.setattr(sentinel_module, "_GIT_HARDENING", ["--not-a-real-git-option"])
    printed = fingerprint(tmp_path)
    first = printed.splitlines()[0]
    assert first.startswith("mode git-unavailable (")
    assert "git exited" in first, first


@needs_git
def test_an_unborn_head_is_a_state_not_a_failure(tmp_path):
    """`git init` with no commit yet. `rev-parse HEAD` fails, and treating that
    as 'git unavailable' would silently drop back to a walk inside a real
    repository."""
    _git(tmp_path, "init", "--quiet")
    printed = fingerprint(tmp_path)
    assert printed.startswith("mode git")
    assert "mode git-unavailable" not in printed
    assert "head unborn" in printed


# --- what CodeRabbit's review added -------------------------------------------


@needs_git
def test_a_linked_worktree_is_covered_at_all(tmp_path):
    """In a linked worktree `.git` is a FILE holding a pointer, not a directory.
    `_git_surface` joined `.git` onto the root and checked `is_dir()`, so it
    returned nothing -- no config, no hooks, no refs -- for exactly the layout
    Whetstone will use once it starts working in worktrees.

    Resolved through `git rev-parse --git-common-dir` now.
    """
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "--quiet")
    _git(main, "config", "user.email", "t@example.invalid")
    _git(main, "config", "user.name", "T")
    (main / "README.md").write_text("start\n", encoding="utf-8")
    _git(main, "add", "README.md")
    _git(main, "commit", "--quiet", "-m", "first", "--no-gpg-sign")

    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "--quiet", str(linked), "-b", "side")
    assert (linked / ".git").is_file(), "the premise: .git is a file here"

    before = fingerprint(linked)
    assert ".git/config" in before, "the execution surface must be covered"

    hooks = main / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-checkout").write_text("#!/bin/sh\necho owned\n", encoding="utf-8")
    assert assert_unchanged(linked, before) is not None


@needs_git
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows forbids '\"' in a filename, so this path cannot exist here",
)
def test_a_path_git_would_c_quote_is_still_hashed(repo):
    """`--porcelain=v1` without `-z` C-quotes any path containing a quote, a
    backslash or a control character. Stripping surrounding quotes does not
    decode those escapes, so `_digest` was handed a path that does not exist and
    returned `absent` for a file sitting right there -- and `absent` is stable,
    so a rewrite of that file was invisible.

    A path is attacker-chosen input; a repository can carry one deliberately.
    """
    awkward = repo / 'we"ird.txt'
    awkward.write_text("original\n", encoding="utf-8")
    before = fingerprint(repo)
    assert "absent" not in before, f"the path was not resolved: {before}"

    awkward.write_text("rewritten by the stage\n", encoding="utf-8")
    assert assert_unchanged(repo, before) is not None


@needs_git
def test_an_ambient_git_environment_cannot_redirect_the_sentinel(repo, monkeypatch):
    """`_GIT_HARDENING` forces off the CONFIG keys that make git execute a
    program. The ENVIRONMENT does the same job by another route and was
    inherited: `GIT_DIR` repoints the repository outright, so a fingerprint
    would describe somewhere else entirely."""
    elsewhere = repo.parent / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "--quiet")

    # OUTSIDE the watched repo. Writing it inside changes the fingerprint by
    # itself, and the comparison below would then prove nothing about GIT_*.
    planted = elsewhere / "planted.gitconfig"
    planted.write_text(
        "[core]\n\tfsmonitor = /nonexistent/evil\n", encoding="utf-8"
    )

    before = fingerprint(repo)
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(elsewhere))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(planted))

    assert fingerprint(repo) == before, (
        "an ambient GIT_* variable changed what the sentinel reports"
    )


def test_the_walk_does_not_descend_into_git(tmp_path):
    """Walk mode is reached whenever git cannot answer, which includes a
    timeout or a stale index.lock INSIDE a real repository. `.git/index`,
    `logs/HEAD` and loose objects move on their own, so walking them made the
    two fingerprints differ and the provider report `the stage is read-only and
    the worktree changed` -- a false accusation against the model."""
    git_dir = tmp_path / ".git"
    (git_dir / "logs").mkdir(parents=True)
    (git_dir / "index").write_bytes(b"before")
    (git_dir / "logs" / "HEAD").write_text("x\n", encoding="utf-8")
    (tmp_path / "real.txt").write_text("a\n", encoding="utf-8")

    before = fingerprint(tmp_path)
    assert "real.txt" in before

    (git_dir / "index").write_bytes(b"after-git-churned-on-its-own")
    (git_dir / "logs" / "HEAD").write_text("y\n", encoding="utf-8")
    assert assert_unchanged(tmp_path, before) is None, (
        "git's own bookkeeping is not the stage mutating the worktree"
    )


def test_the_walk_still_watches_the_git_execution_surface(tmp_path):
    """Pruning `.git` from the traversal must not lose `config` and `hooks`,
    which are how a repository becomes a program and which do not churn."""
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    before = fingerprint(tmp_path)

    (hooks / "post-checkout").write_text("#!/bin/sh\necho owned\n", encoding="utf-8")
    assert assert_unchanged(tmp_path, before) is not None


@needs_git
def test_the_hashed_set_is_capped_and_the_cap_is_recorded(repo, monkeypatch):
    """`--untracked-files=all` lists every untracked file individually, and each
    non-ignored entry was read in full. Whetstone runs against repositories
    whose state it does not choose, so a large uncovered build tree meant two
    full content reads per stage. Measured on Whetstone's own checkout: 3,508
    entries.

    The cap bounds the READING, never the detection -- entries past it still
    carry a stat mark.
    """
    monkeypatch.setattr(sentinel_module, "_HASH_CAP_ENTRIES", 3)
    for index in range(6):
        (repo / f"f{index}.txt").write_text("x\n", encoding="utf-8")

    before = fingerprint(repo)
    assert "first 3 hashed" in before, before.splitlines()[:3]
    # Counted over the STATUS lines only. The `.git` execution surface is hashed
    # separately and is small and fixed, so a bare `sha256:` count across the
    # whole fingerprint measures the hooks directory rather than the cap.
    hashed_status = [
        line
        for line in before.splitlines()
        if line.startswith(("??", " M", "M ", "A ", "!!")) and "sha256:" in line
    ]
    assert len(hashed_status) <= 3, hashed_status

    (repo / "f5.txt").write_text("changed past the cap\n", encoding="utf-8")
    assert assert_unchanged(repo, before) is not None, (
        "the cap must bound the reading, never the detection"
    )
