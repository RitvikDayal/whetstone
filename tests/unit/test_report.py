import os
import re
from pathlib import Path

import pytest

from whetstone.errors import ReportError
from whetstone.report.html import render_report, write_report
from whetstone.runner import RunResult
from whetstone.store.findings import Finding

FINDING = Finding(
    id="1",
    dedupe_key="k",
    lens="hygiene",
    rule_id="GHSA-x",
    subject="requests",
    title="requests 2.19.0 has advisory GHSA-x",
    detail="Header injection.",
    severity="high",
    evidence={"kind": "metric", "summary": "s", "data": {}, "artifacts": []},
    state="queued",
    first_seen_run="run-1",
    last_seen_run="run-1",
    created_at="2026-08-10T10:00:00+00:00",
    updated_at="2026-08-10T10:00:00+00:00",
)

RUN = RunResult(run_id="run-1", tier="quick", file_count=3, status="complete")


def test_report_is_self_contained(assert_self_contained):
    assert_self_contained(render_report([FINDING], project_name="demo", run=RUN))


# --- the self-containment guard, proved non-vacuous by mutation --------------
#
# The shipped assertion was `"https://" not in html.split("</style>")[0]`: one
# literal scheme, in the head only. Each shape below is a real way a report
# stops being self-contained, and seven of the eight passed that assertion
# untouched. `(marker, injection)` -- the injection goes immediately before
# the marker, so style mutations land inside the stylesheet and body ones
# inside the document.

_REGRESSIONS = [
    ("remote-img", "</main>", '<img src="https://cdn.example.com/x.png">'),
    ("remote-iframe", "</main>", '<iframe src="https://evil.example.com/"></iframe>'),
    # Protocol-relative rather than `https://` on purpose: with an https URL
    # the literal lands inside the head and the shipped assertion happened to
    # catch this one shape by accident. The reviewer measured @font-face as
    # undetected, and this is the spelling that reproduces that.
    (
        "web-font",
        "</style>",
        "@font-face { font-family: X; src: url(//fonts.example.com/x.woff2); }",
    ),
    (
        "protocol-relative-url",
        "</style>",
        "body { background: url(//cdn.example.com/bg.png); }",
    ),
    ("css-import", "</style>", '@import url("//cdn.example.com/x.css");'),
    # The first candidate is a data: URI on purpose: it keeps this case about
    # the REMOTE candidate and the descriptor parsing, not about a relative
    # path that would offend on its own.
    (
        "remote-srcset",
        "</main>",
        '<img srcset="data:image/gif;base64,R0lGOD 1x, '
        'https://cdn.example.com/b.png 2x">',
    ),
    (
        "deferred-script",
        "</main>",
        '<script defer src="https://cdn.example.com/x.js"></script>',
    ),
    # Before `<style>`, where a favicon link is actually written. That is also
    # the one position the shipped assertion could see -- it inspected
    # everything up to `</style>` -- which is why this was the single shape of
    # the eight that it caught.
    (
        "remote-favicon",
        "<style>",
        '<link rel="icon" href="https://cdn.example.com/favicon.ico">',
    ),
]


@pytest.mark.parametrize(
    "marker,injection",
    [(marker, injection) for _, marker, injection in _REGRESSIONS],
    ids=[name for name, _, _ in _REGRESSIONS],
)
def test_the_self_containment_guard_catches_every_way_out(
    marker, injection, self_containment_offences
):
    html = render_report([FINDING], project_name="demo", run=RUN)
    assert marker in html, f"template no longer contains {marker!r}"
    broken = html.replace(marker, injection + marker, 1)

    offences = self_containment_offences(broken)
    assert offences, (
        "a deliberately broken template was accepted as self-contained; the "
        "guard is vacuous for this shape"
    )


def test_the_mutation_harness_is_not_just_always_failing(self_containment_offences):
    """The counterweight: an injection that stays inside the document must
    NOT offend, or every case above would pass for the wrong reason."""
    html = render_report([FINDING], project_name="demo", run=RUN)
    benign = html.replace(
        "</main>",
        '<img src="data:image/gif;base64,R0lGOD"><a href="#top">top</a></main>',
        1,
    )
    assert self_containment_offences(benign) == []


def test_report_includes_finding_content():
    html = render_report([FINDING], project_name="demo", run=None)
    assert "requests 2.19.0 has advisory GHSA-x" in html
    assert "Header injection." in html


def test_report_shows_skips_prominently():
    run = RunResult(
        run_id="run-1",
        tier="quick",
        file_count=3,
        status="complete",
        skips=["hygiene/deps: pip-audit is not installed"],
    )
    html = render_report([FINDING], project_name="demo", run=run)
    assert "pip-audit is not installed" in html


def test_empty_report_does_not_claim_clean_when_work_was_skipped():
    run = RunResult(
        run_id="run-1",
        tier="quick",
        file_count=3,
        status="complete",
        skips=["hygiene/deps: skipped"],
    )
    html = render_report([], project_name="demo", run=run)
    assert "Not everything was checked" in html


# --- a run that did not finish must not render as a clean result -------------


@pytest.mark.parametrize("status", ["failed", "running"])
def test_an_unfinished_run_is_not_rendered_as_a_clean_report(status):
    """`get_last_run` did `SELECT *` and read tier/file_count/skipped_json out
    of the row while dropping `status`, and `RunResult` had nowhere to put it.
    Reproduced with a real KeyboardInterrupt during a run: the row said
    status='failed', 0 findings, and `whetstone report` exited 0 with
    "No open findings." and nothing anywhere saying the run never finished.

    An interrupt records NO skip, so the skip banner cannot stand in for this.
    """
    run = RunResult(run_id="run-1", tier="quick", file_count=1, status=status)
    html = render_report([], project_name="demo", run=run)
    assert "did not finish" in html
    assert status in html
    assert "No open findings.</p>" not in html


def test_a_finished_run_with_nothing_to_report_still_says_so_plainly():
    """The counterweight: the warning must not fire on every clean report, or
    it becomes the line nobody reads."""
    html = render_report([], project_name="demo", run=RUN)
    assert "No open findings." in html
    assert "did not finish" not in html


def test_a_report_with_no_run_at_all_does_not_read_as_clean():
    """`get_last_run` returns None for a store no run has ever touched, and
    its docstring says callers must be able to tell that apart from a run
    with no skips 'rather than rendering both as silence'. The template
    rendered both as silence."""
    html = render_report([], project_name="demo", run=None)
    assert "No run has been recorded" in html
    assert "No open findings.</p>" not in html


def test_long_unbroken_detail_text_can_wrap():
    """M0's one real rendering defect: `.detail` set `white-space: pre-wrap`
    and nothing else, so a token with no space in it -- an advisory reference
    URL, a long requirement specifier -- could not break.

    A string assertion is a regression pin, not the proof. The proof is a
    browser: measured in Chrome at a 390px viewport, an advisory ending in a
    GitHub Security Advisory URL gave documentScrollWidth 429 against
    clientWidth 390 before this rule and 390/390 after, and a single
    200,000-character token gave 1,935,097px before and 390px after.

    `"overflow-wrap: anywhere" in css` and `".detail" in css` each hold on
    their own even when the two live in unrelated rules -- restating that
    both constants exist proves neither is attached to the other. Parsed
    into rule blocks instead, so the assertion is that `.detail`'s own block
    is the one carrying the wrapping declaration; a later edit that moves
    the declaration to `.meta` while leaving `.detail` untouched fails this.
    """
    css = render_report([FINDING], project_name="demo", run=RUN).split("</style>")[0]
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    wrapping = [selector for selector, body in rules if "overflow-wrap: anywhere" in body]
    detail_selector = re.compile(r"(?<![\w-])\.detail(?![\w-])")
    assert any(detail_selector.search(selector) for selector in wrapping), css


def test_html_is_escaped():
    hostile = Finding(**{**FINDING.__dict__, "title": "<img src=x onerror=alert(1)>"})
    html = render_report([hostile], project_name="demo", run=None)
    assert "<img src=x" not in html
    assert "&lt;img" in html


# --- write_report: --out is user-supplied and is not trusted blindly ---------
#
# The wizard already refuses to write whetstone.yaml through a symlink (see
# initialize/wizard.py's _refuse_symlinked_target) because a link committed to
# a repository decides where the bytes land on the repository's behalf, not
# the user's. `report --out` is the same shape of problem: a user-supplied
# path that this process writes to unattended.


def test_write_report_refuses_a_symlinked_target(tmp_path, monkeypatch):
    """Faked via monkeypatch, matching test_wizard.py's fallback: an
    unprivileged Windows account cannot always create a real symlink, and this
    variant runs on every platform regardless."""
    target = tmp_path / "report.html"
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: self == target or real_is_symlink(self)
    )
    with pytest.raises(ReportError, match="symlink"):
        write_report(target, "<html></html>", project_root=tmp_path)
    assert not target.exists()


def test_write_report_refuses_a_directory_target(tmp_path):
    target = tmp_path / "report-dir"
    target.mkdir()
    with pytest.raises(ReportError, match="directory"):
        write_report(target, "<html></html>", project_root=tmp_path)


def test_write_report_refuses_to_write_into_dot_git(tmp_path):
    """The entry point behind the barrier rule: `--out .git/config`.

    `write_report` passes an empty `BoundariesConfig` deliberately, so the
    refusal cannot come from `never_touch` and has to be unconditional. The
    assertion is the file's contents, not the exception -- a barrier that
    refuses after truncating has not refused.
    """
    (tmp_path / ".git").mkdir()
    config = tmp_path / ".git" / "config"
    original = "[core]\n\thooksPath = .githooks\n"
    config.write_text(original, encoding="utf-8")
    with pytest.raises(ReportError):
        write_report(config, "<html></html>", project_root=tmp_path)
    assert config.read_text(encoding="utf-8") == original


def test_write_report_wraps_an_os_error_instead_of_raising_raw(tmp_path):
    """A missing parent directory raises FileNotFoundError from write_text;
    that must reach the CLI as a named WhetstoneError, not a bare traceback."""
    target = tmp_path / "does-not-exist" / "report.html"
    with pytest.raises(ReportError):
        write_report(target, "<html></html>", project_root=tmp_path)


def test_write_report_refuses_a_hardlinked_target(tmp_path):
    """A hardlink is not a symlink and is not a redirection.

    `is_symlink()` returns False for it, and there is no target to follow, so
    `cli._report_target`'s `resolve()` sees an ordinary path inside the
    project: both layers pass and the write lands on a file whose other name
    is outside the project. The remaining signal is the link count.
    """
    outside = tmp_path / "outside.html"
    outside.write_text("original", encoding="utf-8")
    inside = tmp_path / "project"
    inside.mkdir()
    target = inside / "report.html"
    os.link(outside, target)

    with pytest.raises(ReportError, match="hardlink"):
        write_report(target, "<html>new</html>", project_root=tmp_path)
    assert outside.read_text(encoding="utf-8") == "original"


def test_write_report_still_accepts_an_ordinary_existing_file(tmp_path):
    """The counterweight to the link-count check: overwriting a report that is
    already there is the normal case and must keep working."""
    target = tmp_path / "report.html"
    target.write_text("old", encoding="utf-8")
    assert write_report(target, "<html>new</html>", project_root=tmp_path) == target
    assert target.read_text(encoding="utf-8") == "<html>new</html>"


@pytest.mark.parametrize(
    "name", ["NUL", "nul", "NUL.html", "con.report.html", "COM1", "lpt9.html"]
)
def test_write_report_refuses_a_reserved_device_name(tmp_path, name):
    """`--out NUL` printed "Wrote ...\\NUL", exited 0, and created nothing.

    Windows resolves these names to a character device in any directory, and
    an extension does not disarm them. Refused on every platform so the guard
    is not one a POSIX test run has to skip past.
    """
    # No `not ...exists()` assertion here: on Windows `Path(tmp)/"nul"` reports
    # that it exists, because the device does. That is the defect, not a
    # side-effect of it.
    with pytest.raises(ReportError, match="reserved device"):
        write_report(tmp_path / name, "<html></html>", project_root=tmp_path)
