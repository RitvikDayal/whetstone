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
  fast (~1s measured) instead of hitting the network for a real dependency
  graph.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from whetstone.cli import app

runner = CliRunner()


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


def test_full_loop_init_doctor_run_findings_report(project, monkeypatch, tmp_path):
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

    report = _invoke("report", "--path", str(project))
    assert report.exit_code == 0
    html = (project / "whetstone-report.html").read_text(encoding="utf-8")
    assert "Whetstone" in html
    assert "https://" not in html.split("</style>")[0]


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
