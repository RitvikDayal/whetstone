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


def test_report_is_self_contained():
    html = render_report([FINDING], project_name="demo", run=None)
    assert "<script src=" not in html
    assert "<link rel=\"stylesheet\" href=http" not in html
    assert "https://" not in html.split("</style>")[0]


def test_report_includes_finding_content():
    html = render_report([FINDING], project_name="demo", run=None)
    assert "requests 2.19.0 has advisory GHSA-x" in html
    assert "Header injection." in html


def test_report_shows_skips_prominently():
    run = RunResult(
        run_id="run-1",
        tier="quick",
        file_count=3,
        skips=["hygiene/deps: pip-audit is not installed"],
    )
    html = render_report([FINDING], project_name="demo", run=run)
    assert "pip-audit is not installed" in html


def test_empty_report_does_not_claim_clean_when_work_was_skipped():
    run = RunResult(
        run_id="run-1", tier="quick", file_count=3, skips=["hygiene/deps: skipped"]
    )
    html = render_report([], project_name="demo", run=run)
    assert "Not everything was checked" in html


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
        write_report(target, "<html></html>")
    assert not target.exists()


def test_write_report_refuses_a_directory_target(tmp_path):
    target = tmp_path / "report-dir"
    target.mkdir()
    with pytest.raises(ReportError, match="directory"):
        write_report(target, "<html></html>")


def test_write_report_wraps_an_os_error_instead_of_raising_raw(tmp_path):
    """A missing parent directory raises FileNotFoundError from write_text;
    that must reach the CLI as a named WhetstoneError, not a bare traceback."""
    target = tmp_path / "does-not-exist" / "report.html"
    with pytest.raises(ReportError):
        write_report(target, "<html></html>")
