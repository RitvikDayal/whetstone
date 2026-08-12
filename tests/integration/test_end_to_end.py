"""The full loop on a synthetic-but-real git repository.

Adapted from task-12-brief.md, which predates a few changes:

- `WhetstoneConfig.state_dir` is now a `SecretStr | None`, not a plain string.
  `test_rejected_finding_is_not_resurrected` needs the real state directory to
  reopen the store, so it unwraps it the same way `cli._load` does --
  `.get_secret_value()` when set, `None` (the home-hash default) otherwise --
  rather than passing the SecretStr straight to `state_root`.
- The fixture's `pyproject.toml` declares a `[project]` table with no
  dependencies. That is deliberate, not an oversight: it is enough for the
  `deps` detector to plan a real `pip-audit` run (proving the subprocess path
  end to end) while resolving zero packages, so the test stays offline and
  fast (~1.3s measured) instead of hitting the network for a real dependency
  graph.

WHAT "PROVING THE SUBPROCESS PATH" NOW MEANS HERE, because the claim above was
made and not asserted. The only finding assertion in this file used to be
`assert "coverage" in findings.stdout.lower()`, so the whole suite passed with
`pip-audit` removed from PATH -- measured, 100% pass, the sole difference an
unchecked skip line. On a runner without pip-audit the subprocess path was
never exercised and nothing said so.

Two assertions replace that, because no single one covers it:

- `test_the_deps_detector_really_ran` asserts the REAL tool ran to completion:
  every failure mode in `DepsDetector.detect` -- absent, timed out, non-zero
  exit, unparseable output -- records a `hygiene/deps:` skip, so the ABSENCE of
  one is positive evidence the real subprocess produced parseable JSON. With
  pip-audit off PATH it fails.
- `test_a_deps_finding_travels_from_the_subprocess_to_the_report` asserts on a
  real `deps` FINDING through the real CLI, with `_PIP_AUDIT_ARGV` pointed at
  a script emitting pip-audit's JSON shape (the seam `test_deps_subprocess.py`
  already uses; Popen, the encoding and the return-code gate all still run).

A real advisory from the real tool needs a real dependency graph and a network
round-trip to the advisory database: measured at 27.8s against a
`requests==2.19.0` pin, versus 1.3s offline. Making the integration suite
depend on that trades a check that never ran for one that fails on a network
blip, which is the same lesson -- a red check nobody trusts is a check nobody
reads. The realism it costs is bought back by the first assertion, which uses
the genuine tool.

`pip-audit`'s absence FAILS rather than skips. It is a declared dev dependency
(see pyproject's `[dependency-groups]`), so a run without it is a broken
environment, not a host limitation.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from whetstone.cli import app
from whetstone.lenses.hygiene.detectors import deps as deps_module

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch):
    """Pin Rich's width so assertions about its output are not width-coupled.

    `assert "coverage" in findings.stdout.lower()` reads text Rich rendered
    into a TABLE, and a narrow terminal makes Rich wrap the cell and split the
    word. Measured at COLUMNS=40: the subject column is eight characters wide
    and the assertion fails on terminal width rather than on behaviour. CI
    leaves Rich at its 80-column default so this was unlikely to bite, but
    "passed locally, failed on CI" is a shape to remove rather than bet on.

    Sets the width on the console OBJECT, not `COLUMNS` in the environment.
    Rich reads `COLUMNS` inside `Console.__init__` and caches it into
    `_width`, and `whetstone.cli`'s console is built at import time -- so
    `monkeypatch.setenv("COLUMNS", ...)` reaches it only when COLUMNS happened
    to be UNSET when the module was imported, and silently does nothing
    otherwise. That is the same class of defect as the finding it fixes: a
    guard that works only under conditions nobody checks. `_width` is what
    Rich's own public `width` setter assigns; going through the attribute
    gets monkeypatch's restoration for free.
    """
    from whetstone.cli import console

    monkeypatch.setattr(console, "_width", 300)


def test_pip_audit_is_installed():
    """A declared dev dependency. Its absence is a broken environment, and
    this fails rather than skips so the suite cannot go green with the deps
    path silently unexercised -- the fifth instance on this project of a
    check that quietly did not run."""
    assert shutil.which("pip-audit") is not None, (
        "pip-audit is not on PATH. It is a dev dependency (`uv sync "
        "--all-groups`); without it the deps detector skips and every "
        "assertion about it below becomes vacuous."
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0"?><coverage line-rate="0.30"/>', encoding="utf-8"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init", "--no-gpg-sign")
    return tmp_path


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point `state_root`'s home-hash default at a throwaway directory.

    `state_root` falls back to `Path.home() / ".whetstone" / <digest>` for
    every project in this fixture (none of them set `state_dir`), and without
    this every one of these tests would read and write the real
    `~/.whetstone` on whatever machine runs them.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


def test_full_loop_init_doctor_run_findings_report(
    project, monkeypatch, tmp_path, assert_self_contained
):
    _isolate_home(monkeypatch, tmp_path)

    init = _invoke("init", "--path", str(project), "--yes")
    assert init.exit_code == 0, init.stdout
    assert (project / "whetstone.yaml").is_file()

    doctor = _invoke("doctor", "--path", str(project))
    assert doctor.exit_code == 0, doctor.stdout
    assert "state path" in doctor.stdout

    run = _invoke("run", "--path", str(project), "--full")
    assert run.exit_code == 0, run.stdout
    assert "new" in run.stdout

    findings = _invoke("findings", "--path", str(project))
    assert findings.exit_code == 0
    assert "coverage" in findings.stdout.lower()
    # The run finished, so nothing may claim otherwise.
    assert "did not finish" not in findings.stdout

    report = _invoke("report", "--path", str(project))
    assert report.exit_code == 0
    html = (project / "whetstone-report.html").read_text(encoding="utf-8")
    assert "Whetstone" in html
    assert "did not finish" not in html
    assert_self_contained(html)


def test_the_deps_detector_really_ran(project, monkeypatch, tmp_path):
    """The real pip-audit subprocess, asserted rather than assumed.

    Every failure mode in `DepsDetector.detect` -- tool absent, timeout,
    non-zero exit, empty output, unparseable JSON, unexpected shape -- records
    a skip prefixed `hygiene/deps:`. So no such skip means the real tool was
    spawned, exited acceptably, and returned JSON this code could walk. With
    pip-audit removed from PATH the suite used to stay 100% green; it now
    fails here.
    """
    _isolate_home(monkeypatch, tmp_path)
    _invoke("init", "--path", str(project), "--yes")

    run = _invoke("run", "--path", str(project), "--full")
    assert run.exit_code == 0, run.stdout
    assert "hygiene/deps" not in run.stdout, (
        "the deps detector recorded a skip, so the real pip-audit subprocess "
        f"did not complete:\n{run.stdout}"
    )


# One advisory in pip-audit's own JSON shape. Same seam test_deps_subprocess.py
# uses: CreateProcess resolves only `.exe` from PATH on Windows, so a shim
# cannot shadow the tool portably, and replacing the argv prefix is the
# equivalent that still runs Popen, the encoding and the return-code gate.
_ONE_ADVISORY = {
    "dependencies": [
        {
            "name": "vulnerable-demo",
            "version": "1.0.0",
            "vulns": [
                {
                    "id": "PYSEC-DEMO-1",
                    "fix_versions": ["1.0.1"],
                    "description": "A demonstration advisory.",
                }
            ],
        }
    ]
}


def test_a_deps_finding_travels_from_the_subprocess_to_the_report(
    project, monkeypatch, tmp_path
):
    """A `deps` finding asserted end to end: subprocess -> store -> CLI -> HTML.

    The suite had no assertion on a deps finding at all, so the detector's
    whole output path was unexercised through the real commands. A real
    advisory needs a network round-trip (27.8s measured); this keeps the
    subprocess real and controls only what it prints.
    """
    _isolate_home(monkeypatch, tmp_path)
    script = tmp_path / "fake_pip_audit.py"
    script.write_text(
        f"import json\nprint(json.dumps({_ONE_ADVISORY!r}))\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", (sys.executable, str(script))
    )

    assert _invoke("init", "--path", str(project), "--yes").exit_code == 0
    run = _invoke("run", "--path", str(project), "--full")
    assert run.exit_code == 0, run.stdout
    assert "hygiene/deps" not in run.stdout, run.stdout

    findings = _invoke("findings", "--path", str(project))
    assert findings.exit_code == 0, findings.stdout
    assert "vulnerable-demo" in findings.stdout
    assert "PYSEC-DEMO-1" in findings.stdout

    out = project / "deps-report.html"
    report = _invoke("report", "--path", str(project), "--out", str(out))
    assert report.exit_code == 0, report.stdout
    html = out.read_text(encoding="utf-8")
    assert "PYSEC-DEMO-1" in html
    assert "A demonstration advisory." in html
    assert "No open findings" not in html


def test_the_fake_audit_seam_actually_replaced_the_real_tool(
    project, monkeypatch, tmp_path
):
    """The counterweight to the test above: if the monkeypatch silently did
    nothing, the real pip-audit would run, find nothing, and the assertions
    would have to be measuring something else. A script that emits invalid
    JSON must produce the detector's own unparseable-output skip."""
    _isolate_home(monkeypatch, tmp_path)
    script = tmp_path / "broken_pip_audit.py"
    script.write_text("print('not json at all')\n", encoding="utf-8")
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", (sys.executable, str(script))
    )

    assert _invoke("init", "--path", str(project), "--yes").exit_code == 0
    run = _invoke("run", "--path", str(project), "--full")
    assert "unparseable JSON" in run.stdout, run.stdout


def test_second_run_reports_nothing_new(project, monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)

    _invoke("init", "--path", str(project), "--yes")
    first = _invoke("run", "--path", str(project), "--full")
    assert first.exit_code == 0, first.stdout

    second = _invoke("run", "--path", str(project), "--full")
    assert second.exit_code == 0, second.stdout
    assert "0 new" in second.stdout


def test_rejected_finding_is_not_resurrected(project, monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)

    from whetstone.config.loader import find_config, load_config
    from whetstone.paths import state_root
    from whetstone.store.db import connect

    init = _invoke("init", "--path", str(project), "--yes")
    assert init.exit_code == 0, init.stdout
    run1 = _invoke("run", "--path", str(project), "--full")
    assert run1.exit_code == 0, run1.stdout

    cfg = load_config(find_config(project))
    # `state_dir` is a SecretStr | None (see module docstring); `state_root`
    # wants the plain string it wraps, the same unwrap `cli._load` performs.
    override = cfg.state_dir.get_secret_value() if cfg.state_dir is not None else None
    conn = connect(state_root(project, override))

    rows_before = conn.execute("SELECT state FROM findings").fetchall()
    assert rows_before, "expected at least one finding to reject -- fixture regressed"
    conn.execute("UPDATE findings SET state = 'rejected'")

    run2 = _invoke("run", "--path", str(project), "--full")
    assert run2.exit_code == 0, run2.stdout

    states = [row[0] for row in conn.execute("SELECT state FROM findings")]
    assert states and all(state == "rejected" for state in states)
