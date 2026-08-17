"""The GitHub sinks.

WHAT IS WORTH ATTACKING: publication is the most consequential thing this tool
does, and it happens under somebody else's credentials in somebody else's
repository. So the tests below concentrate on the paths where something gets
published that should not have been -- an unverified fix, a lens that has not
earned it, a ready PR where a draft was owed -- and on the ways a failure could
be reported as a success.

`gh` is never really invoked. The seam is `_run`, which is the same shape
`test_deps_subprocess.py` uses for `pip-audit`: the argv is built for real and
asserted on, and only the process boundary is replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.sinks import github
from whetstone.sinks.base import Publication, SinkError


class _Finding:
    id = "abc123"
    title = "Division by zero on an empty list"
    subject = "orders.py:9"
    lens = "code-defects"
    severity = "medium"
    detail = "average_price divides by len(prices) with no guard."
    grade = "A"
    grade_reason = "graded A: reproduced, survived falsification."
    evidence = {
        "data": {
            "falsify": {"strongest_counterargument": "Callers may guarantee non-empty."},
            "reproduction": {"verdict": "reproduced"},
        }
    }


@pytest.fixture
def calls(monkeypatch):
    """Capture the argv and return a scripted result."""
    recorded: list[list[str]] = []
    outcome = {"code": 0, "out": "https://github.com/o/r/issues/1", "err": ""}

    def _fake(argv, cwd=None):
        recorded.append(argv)
        return outcome["code"], outcome["out"], outcome["err"]

    monkeypatch.setattr(github, "_run", _fake)
    return recorded, outcome


def _issues(tmp_path: Path) -> github.GitHubIssues:
    return github.GitHubIssues(repo="o/r", project_root=tmp_path)


def _prs(tmp_path: Path) -> github.GitHubPullRequest:
    return github.GitHubPullRequest(repo="o/r", project_root=tmp_path)


# --- nothing merges ---------------------------------------------------------------


def test_no_sink_can_merge():
    """Asserted here as well as in test_invariants, because this is the module
    where somebody would add it.

    Scans the CODE, with docstrings and comments removed. The first version
    scanned raw text and failed on this module's own docstring, which names
    `gh pr merge` while explaining that it is absent -- the same self-reference
    hazard `test_invariants.py` handles by excluding itself from its own scan.
    Rewording the prose to dodge the scan would have been the wrong fix: the
    explanation is worth more than the convenience.
    """
    import ast

    tree = ast.parse(Path(github.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value.value = ""  # blank every docstring
    code = ast.unparse(tree)
    for forbidden in ("pr merge", "--merge", "--auto", "--admin"):
        assert forbidden not in code, forbidden


# --- a Publication cannot lie about itself ----------------------------------------


def test_a_success_with_no_url_is_refused():
    """Every caller renders this as a link. A publication nobody can open is
    not one."""
    with pytest.raises(SinkError, match="no URL"):
        Publication(published=True, kind="github-issues")


def test_a_failure_with_no_reason_is_refused():
    with pytest.raises(SinkError, match="no reason"):
        Publication(published=False, kind="github-issues")


# --- the PR sink refuses more than it accepts -------------------------------------


def test_an_unverified_fix_is_never_published(tmp_path, calls):
    """The gate that matters. Opening a PR is the most consequential thing this
    tool does, and `verified` was computed by replaying the finding's own
    evidence rather than by anybody asserting the fix works."""
    recorded, _ = calls
    result = _prs(tmp_path).publish(
        _Finding(), branch="whetstone/r1", verified=False, level=3
    )
    assert result.published is False
    assert "not verified" in result.reason
    assert recorded == [], "gh was invoked for an unverified fix"


@pytest.mark.parametrize("level", [0, 1])
def test_a_lens_below_draft_level_publishes_nothing(tmp_path, calls, level):
    """Levels 0 and 1 are report and propose. Neither is a pull request."""
    recorded, _ = calls
    result = _prs(tmp_path).publish(
        _Finding(), branch="whetstone/r1", verified=True, level=level
    )
    assert result.published is False
    assert f"autonomy {level}" in result.reason
    assert "whetstone/r1" in result.reason, "the branch must be named so a human can take it"
    assert recorded == []


def test_no_branch_means_nothing_to_open(tmp_path, calls):
    recorded, _ = calls
    result = _prs(tmp_path).publish(_Finding(), branch=None, verified=True, level=3)
    assert result.published is False
    assert recorded == []


# --- draft at 2, ready at 3 ---------------------------------------------------------


def test_level_two_opens_a_draft(tmp_path, calls):
    recorded, outcome = calls
    outcome["out"] = "https://github.com/o/r/pull/7"
    result = _prs(tmp_path).publish(
        _Finding(), branch="whetstone/r1", verified=True, level=2
    )
    assert result.published is True
    assert "--draft" in recorded[0]
    assert result.detail["draft"] is True


def test_level_three_opens_a_ready_pull_request(tmp_path, calls):
    recorded, outcome = calls
    outcome["out"] = "https://github.com/o/r/pull/7"
    result = _prs(tmp_path).publish(
        _Finding(), branch="whetstone/r1", verified=True, level=3
    )
    assert result.published is True
    assert "--draft" not in recorded[0]


def test_the_pull_request_targets_the_configured_base(tmp_path, calls):
    recorded, outcome = calls
    outcome["out"] = "https://github.com/o/r/pull/7"
    sink = github.GitHubPullRequest(
        repo="o/r", project_root=tmp_path, base_branch="develop"
    )
    sink.publish(_Finding(), branch="whetstone/r1", verified=True, level=3)
    assert "--base" in recorded[0]
    assert recorded[0][recorded[0].index("--base") + 1] == "develop"


# --- failures are failures ----------------------------------------------------------


def test_a_failed_gh_call_is_not_a_publication(tmp_path, calls):
    _, outcome = calls
    outcome.update(code=1, out="", err="could not resolve to a Repository")
    result = _issues(tmp_path).publish(_Finding())
    assert result.published is False
    assert "could not resolve" in result.reason


def test_a_failed_pr_creation_is_not_a_publication(tmp_path, calls):
    """The PR path has its own failure branch, and only the issue path was
    covered -- so reporting a failed `gh pr create` as a success survived a
    battery. This is the more consequential of the two: a run that believes it
    opened a pull request will not open one later."""
    _, outcome = calls
    outcome.update(code=1, out="", err="head branch does not exist")
    result = _prs(tmp_path).publish(
        _Finding(), branch="whetstone/r1", verified=True, level=3
    )
    assert result.published is False
    assert "head branch does not exist" in result.reason
    assert result.url is None


def test_a_missing_gh_is_not_reported_as_an_authentication_problem(monkeypatch):
    """Two different fixes. Telling somebody to log in when the binary is not
    installed sends them somewhere that cannot help, and asserting only that
    the message mentions PATH passed either way -- the fallback branch quotes
    stderr, which already said PATH."""
    monkeypatch.setattr(
        github, "_run", lambda argv, cwd=None: (127, "", "the `gh` CLI is not on PATH")
    )
    message = github.available()
    assert "not on PATH" in message
    assert "not authenticated" not in message


def test_an_unauthenticated_gh_is_not_available(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda argv, cwd=None: (1, "", "not logged in"))
    assert "not authenticated" in github.available()


def test_available_returns_none_when_it_can_publish(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda argv, cwd=None: (0, "Logged in", ""))
    assert github.available() is None


# --- the body is the finding, not a rewriting of it -----------------------------------


def test_the_issue_body_carries_the_grade_and_the_counterargument(tmp_path, calls):
    """No model is on the publication path. Every line comes from a stored
    field, and the counterargument is included because a reader who cannot see
    what this survived cannot judge whether to trust it."""
    recorded, _ = calls
    _issues(tmp_path).publish(_Finding())
    body = recorded[0][recorded[0].index("--body") + 1]
    assert "graded A" in body
    assert "Callers may guarantee non-empty" in body
    assert "orders.py:9" in body


def test_the_pr_body_states_what_was_verified(tmp_path, calls):
    recorded, outcome = calls
    outcome["out"] = "https://github.com/o/r/pull/7"
    _prs(tmp_path).publish(_Finding(), branch="b", verified=True, level=3)
    body = recorded[0][recorded[0].index("--body") + 1]
    assert "regression test fails without this change" in body
    assert "never merges" in body


def test_the_argv_is_a_list_so_a_shell_never_reparses_a_title(tmp_path, calls):
    """A finding's title is model-authored text containing whatever it
    contains. Flattening the argv lets the host shell re-parse every quote in
    it, which broke differently on the Windows and Ubuntu legs the last time
    this codebase did it."""
    recorded, _ = calls

    class _Hostile(_Finding):
        title = 'x"; rm -rf /; echo "'

    _issues(tmp_path).publish(_Hostile())
    argv = recorded[0]
    assert isinstance(argv, list)
    assert any('rm -rf' in part for part in argv), "the title should travel intact"
    assert all(isinstance(part, str) for part in argv)


# --- dry run ---------------------------------------------------------------------------


def test_a_dry_run_publishes_nothing_but_shows_the_argv(tmp_path, calls):
    recorded, _ = calls
    result = _issues(tmp_path).publish(_Finding(), dry_run=True)
    assert result.published is False
    assert "dry run" in result.reason
    assert recorded == []
    assert result.detail["argv"][:3] == ["gh", "issue", "create"]
