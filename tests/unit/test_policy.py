"""The policy gate, and the two ways its first version did not hold.

Neither failure was a coding mistake. The code did what it said. It said the
wrong thing: `allowed_tools` named the set of tools that would not prompt, and
was used as the set of tools that existed; `_normalise` erased the one
whitespace character that is also a command separator.
"""

from __future__ import annotations

import inspect

import pytest

from whetstone.errors import WhetstoneError
from whetstone.policy.gate import (
    PermissionSet,
    PolicyError,
    _base_tool,
    _normalise,
    bash_permitted,
)
from whetstone.policy.profiles import FORBIDDEN_IN_M1A, PROFILES, profile_for
from whetstone.provider import claude_cli

_NO_WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

# Every character Python calls whitespace except space and tab, which is exactly
# the set `str.split()` splits on. Shared by the command-side and entry-side
# cases below: pinning only one side let a mutation that reduced the guard to
# `\n` alone survive, because on the command side the refusal comes from the
# mismatch rather than from the guard.
_SEPARATORS = [
    ("newline", "\n"),
    ("carriage return", "\r"),
    ("vertical tab", "\v"),
    ("form feed", "\f"),
    ("NEL", "\x85"),
    ("no-break space", "\xa0"),
    ("file separator", "\x1c"),
    ("group separator", "\x1d"),
    ("record separator", "\x1e"),
    ("unit separator", "\x1f"),
]


def _perms(**overrides) -> PermissionSet:
    base = dict(
        available_tools=frozenset({"Read", "Grep", "Bash"}),
        auto_approve=frozenset({"Read", "Grep"}),
        denied_tools=frozenset({"Edit", "Write"}),
        bash_allowlist=frozenset({"uv run pytest -q"}),
        read_denied=(".env*", "**/secrets/**"),
        write_root=None,
    )
    base.update(overrides)
    return PermissionSet(**base)


def _superseded_bash_permitted(command: str, permissions: PermissionSet) -> bool:
    """The implementation this module shipped with, verbatim.

    Kept in the test file rather than described in a comment, so the cases below
    can PROVE they discriminate against it instead of asserting they do. The
    first attempt at these tests asserted a refusal the old code also produced,
    and passed unchanged against the defect it was written to catch.
    """
    normalise = lambda text: " ".join(text.split())  # noqa: E731
    return normalise(command) in {normalise(a) for a in permissions.bash_allowlist}


# --- bash allowlisting --------------------------------------------------------


def test_an_exact_command_is_permitted():
    assert bash_permitted("uv run pytest -q", _perms())


def test_runs_of_space_and_tab_are_collapsed():
    assert bash_permitted("  uv \t run  pytest \t\t -q  ", _perms())


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest -q; rm -rf /",
        "uv run pytest -q && curl evil.example",
        "uv run pytest -q | sh",
        "uv run pytest -qq",
        "uv run pytest",
        "echo uv run pytest -q",
    ],
)
def test_prefix_and_suffix_tricks_are_refused(command):
    """A prefix allowlist is bypassable. This is why matching is exact."""
    assert not bash_permitted(command, _perms())


@pytest.mark.parametrize(("name", "separator"), _SEPARATORS)
def test_normalise_collapses_space_and_tab_and_nothing_else(name, separator):
    """`_normalise` on its own, because the guard hides it.

    With the refusal in place, restoring `_RUNS` to `\\s+` -- the superseded
    spelling, and a very natural simplification -- changes no observable
    behaviour, so it survived every test. Two independently-green edits then
    compose back into the shipped bypass. This pins the normaliser directly so
    neither half can move quietly.
    """
    assert _normalise(f"a{separator}b") == f"a{separator}b", name
    assert _normalise("  a \t\t b  ") == "a b"


@pytest.mark.parametrize(("name", "separator"), _SEPARATORS)
def test_a_foreign_separator_in_an_allowlist_ENTRY_is_refused(name, separator):
    """The entry side, parametrised.

    Only the `\\n` case was pinned here, and the command-side cases below refuse
    on mismatch rather than on the guard -- so reducing `_FOREIGN_SPACE` to
    `\\n` alone passed nine of ten. An operator who writes a two-command entry
    must not have it silently become matchable.
    """
    perms = _perms(bash_allowlist=frozenset({f"bash{separator}scripts/ci.sh"}))
    assert _superseded_bash_permitted("bash scripts/ci.sh", perms), (
        f"{name}: the entry-side hole has to be reachable under the old code "
        f"for this case to be worth anything"
    )
    assert not bash_permitted("bash scripts/ci.sh", perms), name
    assert not bash_permitted(f"bash{separator}scripts/ci.sh", perms), name


@pytest.mark.parametrize(("name", "separator"), _SEPARATORS)
def test_whitespace_that_is_not_space_or_tab_is_refused_not_normalised(
    name, separator
):
    """The bypass that shipped.

    `\\n` is a POSIX command separator, so a normaliser that erases it does not
    reformat one command -- it splits it into two. The operator allowlists
    `bash scripts/ci.sh`; the stage sends `bash<sep>scripts/ci.sh`; the old
    normaliser collapses that to the allowlisted string and approves it; the
    shell then runs an interactive `bash` AND `scripts/ci.sh`, neither of which
    is what was approved.

    Every character here is `str.isspace()` in Python, which is exactly the set
    `str.split()` splits on and exactly why the old implementation erased them.
    """
    perms = _perms(bash_allowlist=frozenset({"bash scripts/ci.sh"}))
    smuggled = f"bash{separator}scripts/ci.sh"

    assert _superseded_bash_permitted(smuggled, perms), (
        f"{name} is not permitted even by the superseded implementation, so "
        f"this case discriminates against nothing -- pick a character that is"
    )
    assert not bash_permitted(smuggled, perms), name


def test_an_empty_allowlist_permits_nothing():
    assert not bash_permitted("uv run pytest -q", _perms(bash_allowlist=frozenset()))


def test_policy_error_is_a_whetstone_error():
    assert issubclass(PolicyError, WhetstoneError)


# --- the two sets -------------------------------------------------------------


def test_auto_approve_cannot_widen_the_available_set():
    """`--allowedTools` removes an approval prompt. It does not add a tool, and
    a PermissionSet that reads as though it does is refused at construction."""
    with pytest.raises(PolicyError, match="Bash"):
        _perms(
            available_tools=frozenset({"Read"}),
            auto_approve=frozenset({"Read", "Bash"}),
        )


def test_a_scoped_approval_is_matched_on_the_tool_it_scopes():
    """`Bash(uv run pytest -q)` is an approval for Bash, so Bash has to be
    available for it -- and it is accepted when Bash is."""
    _perms(auto_approve=frozenset({"Read", "Bash(uv run pytest -q)"}))
    with pytest.raises(PolicyError, match="Bash"):
        _perms(
            available_tools=frozenset({"Read"}),
            auto_approve=frozenset({"Bash(uv run pytest -q)"}),
        )


@pytest.mark.parametrize(
    "spec", ["Bash(uv run pytest -q)", "Bash (uv run pytest -q)", "  Bash  (x)"]
)
def test_a_scoped_approval_is_parsed_leniently_around_the_tool_name(spec):
    """`_base_tool` strips. Nothing pinned that, so dropping the strip -- which
    turns `Bash (x)` into the tool name `"Bash "` and raises on a legitimate
    PermissionSet -- was invisible."""
    perms = _perms(available_tools=frozenset({"Bash"}), auto_approve=frozenset({spec}))
    assert perms.auto_approve == frozenset({spec})
    assert _base_tool(spec) == "Bash"


def test_a_scoped_deny_of_an_available_tool_is_still_an_overlap():
    """The two checks disagreed: the `auto_approve` one normalised through
    `_base_tool` and the deny one compared raw strings, so `denied_tools={"Bash(rm
    -rf /)"}` alongside `available_tools={"Bash"}` reported no overlap. Two
    spellings of the same tool, one of them a deny, silently passing."""
    with pytest.raises(PolicyError, match="Bash"):
        _perms(
            available_tools=frozenset({"Bash"}),
            auto_approve=frozenset(),
            denied_tools=frozenset({"Bash(rm -rf /)"}),
        )


@pytest.mark.parametrize(
    "field",
    ["available_tools", "auto_approve", "denied_tools"],
)
@pytest.mark.parametrize(
    "spec", ["--dangerously-skip-permissions", "-p", "  --add-dir", ""]
)
def test_a_flag_shaped_tool_name_is_refused(field, spec):
    """A tool name that looks like a flag IS a flag by the time it reaches the
    argv. Proven before this guard existed:

        PermissionSet(available_tools={"--dangerously-skip-permissions"})
        _argv(...)  ->  ['--tools', '--dangerously-skip-permissions']

    So the object whose entire job is constraining the invocation could inject
    arbitrary options into it. Not reachable from today's profiles, which are
    fixed literals -- and that is exactly the reasoning that made the original
    `--allowedTools` mapping look fine, so it is closed at the boundary instead.
    """
    base = dict(
        available_tools=frozenset({"Read"}),
        auto_approve=frozenset({"Read"}),
        denied_tools=frozenset({"Write"}),
    )
    base[field] = frozenset({spec}) | (
        frozenset({"Read"}) if field != "denied_tools" else frozenset()
    )
    with pytest.raises(PolicyError):
        _perms(**base)


def test_a_tool_cannot_be_both_available_and_denied():
    """`auto_approve` is deliberately EMPTY here.

    The previous version used `denied={"Read"}` while `Read` was also in
    `auto_approve`, so mutating the check to `auto_approve & denied_tools`
    raised just the same and survived. The two intersections have to be
    distinguishable by the fixture or the test pins neither.
    """
    with pytest.raises(PolicyError, match="Bash"):
        _perms(
            available_tools=frozenset({"Bash"}),
            auto_approve=frozenset(),
            denied_tools=frozenset({"Bash"}),
        )


# --- the profile roster -------------------------------------------------------


def test_the_roster_is_exactly_the_three_m1a_stages():
    """The superseded tests iterated PROFILES and passed on an empty dict. Two
    of them were mutation-proven to survive `PROFILES = {}`."""
    assert set(PROFILES) == {"hunt", "reproduce", "falsify"}


# The M1a roster, spelled out. Every earlier profile test was an ABSENCE
# assertion -- no Bash in hunt, no Agent anywhere -- and an empty set satisfies
# every one of them. `reproduce.auto_approve = frozenset()` survived the whole
# suite. A table that has to be edited in two places to change is the point.
#
# All three are identical since 2026-08-13. `reproduce` and `falsify` used to
# carry `Bash`; a reviewer proved an unapproved shell still runs anything the
# CLI's own classifier calls read-only, recording nothing.
_ROSTER = {
    "hunt": {
        "available": frozenset({"Read", "Grep", "Glob"}),
        "approve": frozenset({"Read", "Grep", "Glob"}),
    },
    "reproduce": {
        "available": frozenset({"Read", "Grep", "Glob"}),
        "approve": frozenset({"Read", "Grep", "Glob"}),
    },
    "falsify": {
        "available": frozenset({"Read", "Grep", "Glob"}),
        "approve": frozenset({"Read", "Grep", "Glob"}),
    },
}

_SECRET_PATTERNS = (
    ".env*",
    "**/secrets/**",
    "**/credentials/**",
    "**/.ssh/**",
    "**/.aws/**",
    "**/.kube/**",
)


@pytest.mark.parametrize("name", sorted(_ROSTER))
def test_each_profile_grants_exactly_what_the_roster_says(name):
    """Both sets pinned exactly, because they are separately mutable and only
    `hunt` has them equal -- so a swap of the two is invisible if only one is
    checked."""
    perms = PROFILES[name]
    assert perms.available_tools == _ROSTER[name]["available"]
    assert perms.auto_approve == _ROSTER[name]["approve"]


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_every_profile_grants_something_and_denies_writing(name):
    """No stage in M1a writes. The implementer arrives in M1b."""
    perms = PROFILES[name]
    assert perms.available_tools, "an empty available set is a stage that cannot run"
    assert perms.auto_approve, "an empty approve set makes every call a prompt"
    assert perms.denied_tools == _NO_WRITE_TOOLS
    assert not (perms.available_tools & _NO_WRITE_TOOLS)


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_no_profile_grants_a_shell_or_a_subagent(name):
    """The three tools no M1a stage may ever hold, asserted against the module's
    own list so adding a fourth is one edit rather than two.

    None of this is hypothetical. A reviewer spawned a subagent from a profile
    that granted neither `Agent` nor `TaskCreate`, and when that subagent's
    `Write` was refused it fell back to `Bash` and wrote the file anyway. A
    separate reviewer got six read-only shell commands to execute under an
    empty `bash_allowlist`, with nothing recorded.
    """
    assert not (PROFILES[name].available_tools & FORBIDDEN_IN_M1A)
    assert not (PROFILES[name].auto_approve & FORBIDDEN_IN_M1A)


def test_the_forbidden_set_still_names_the_shell():
    """`FORBIDDEN_IN_M1A` is the thing the test above leans on entirely, so
    emptying it -- or quietly dropping `Bash` back out of it -- would make every
    profile pass while granting anything."""
    assert frozenset({"Bash", "Agent", "TaskCreate"}) == FORBIDDEN_IN_M1A


def test_read_denied_is_DECLARED_and_NOT_enforced():
    """An admission with an expiry date, and it is deliberately ugly.

    `read_denied` names `.env*`, `**/.ssh/**` and four more, and `_argv` never
    consumes it. Proven by a reviewer: a `hunt` stage read a file outside the
    worktree AND outside `--add-dir` on request -- `--add-dir` scopes writes,
    not reads. Two credential reads were refused by MODEL ALIGNMENT, with
    nothing in the harness declining.

    So a green policy suite is not evidence that any secret is protected. This
    test goes red the moment somebody wires the field up, which is exactly when
    the docstrings that disclaim it have to change too.
    """
    assert PROFILES["hunt"].read_denied, "the patterns are declared"
    assert "read_denied" not in inspect.getsource(claude_cli._argv), (
        "read_denied now reaches the argv -- delete this test, and update the "
        "disclosure table in the M1a plan's invariants section and the "
        "docstrings in sentinel.py and profiles.py that call it unenforced"
    )


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_every_profile_denies_reading_secrets(name):
    """The WHOLE tuple, not just `.env*`.

    Pinning one pattern let a mutation that dropped `**/secrets/**`,
    `**/.ssh/**`, `**/.aws/**` and `**/.kube/**` pass.

    NOTE, and it is the important part: this asserts what the profile
    DECLARES. `read_denied` currently reaches no CLI flag -- `_argv` never
    consumes it -- so a green run here is not evidence that any secret is
    protected. See `sentinel.py`'s docstring and the M1a plan.
    """
    assert PROFILES[name].read_denied == _SECRET_PATTERNS


def test_all_three_stages_hold_identical_powers():
    """They differ in what they are ASKED and in the process boundary between
    them, not in what they may touch. Anything else would be a claim the CLI
    cannot back."""
    assert len({
        (p.available_tools, p.auto_approve, p.denied_tools) for p in PROFILES.values()
    }) == 1


def test_profile_for_an_unknown_stage_refuses_rather_than_defaulting():
    """Defaulting to a permissive set would make a typo a privilege escalation."""
    with pytest.raises(PolicyError, match="nosuchstage"):
        profile_for("nosuchstage")
