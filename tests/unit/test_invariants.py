"""Invariants CLAUDE.md asserts are held by a test. This is that test.

Whetstone never merges and never deploys. That is a design promise, not an
oversight, and it held only by nobody having written the code yet. Scans src/
for the commands themselves and for the argument-list spellings a subprocess
call would use.

This file names every forbidden string, so it scans src/ only and never itself.

Scope: this is a static scan of source TEXT. It proves Whetstone's own code
contains no literal call to these commands -- it does NOT prove no such
command can ever execute. `doctor.py` runs arbitrary user-declared strings
under `shell=True` by design (see its module docstring), so a whetstone.yaml
declaring `test: git push origin main` runs under doctor and this guard stays
green; the scan cannot see through `shell=True` into a runtime string. That
is the intended boundary.

Authorship (human-written config vs. model-authored) is the boundary for the
command STRING, and it is not the boundary for what actually runs: `shell=True`
with `cwd=project_root` lets cmd.exe resolve a `git.bat` in the project root
ahead of `git.EXE` on PATH, so even a string nobody would object to can execute
the repository's own code. doctor.py's module docstring carries the measurement.
Do not restate the boundary as authorship alone -- this file said that, and it
was wrong in a way that reads as reassuring.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
WORKFLOWS = ROOT / ".github" / "workflows"

# (label, pattern). Each covers the shell-string form and the argv-list form:
# subprocess.run(["git", "push", ...]) has to fail too.
_FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("git merge", re.compile(r"""git\s+merge\b|["']git["']\s*,\s*["']merge["']""")),
    ("git push", re.compile(r"""git\s+push\b|["']git["']\s*,\s*["']push["']""")),
    (
        "gh pr merge",
        re.compile(r"""gh\s+pr\s+merge\b|["']pr["']\s*,\s*["']merge["']"""),
    ),
    (
        "kubectl apply",
        re.compile(r"""kubectl\s+apply\b|["']kubectl["']\s*,\s*["']apply["']"""),
    ),
    (
        "terraform apply",
        re.compile(r"""terraform\s+apply\b|["']terraform["']\s*,\s*["']apply["']"""),
    ),
)


def _sources() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py")) if "__pycache__" not in p.parts]


def test_the_scan_actually_reaches_the_source_tree():
    """A guard test that silently scans nothing is worse than no guard at all."""
    assert SRC.is_dir(), f"{SRC} is not a directory"
    found = _sources()
    assert len(found) >= 5, f"only found {found}"


@pytest.mark.parametrize("label,pattern", _FORBIDDEN, ids=[f[0] for f in _FORBIDDEN])
def test_nothing_under_src_can_merge_or_deploy(label, pattern):
    offences = []
    for source in _sources():
        for number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offences.append(f"{source}:{number}: {line.strip()}")
    assert not offences, (
        f"src/ must never invoke `{label}`. Whetstone reports and proposes; the "
        "human merges and deploys.\n" + "\n".join(offences)
    )


# .coderabbit.yaml tells reviewers to flag third-party actions pinned by tag
# rather than commit SHA. The repo's own workflow was doing exactly that.
_USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)")
_PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _workflows() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def test_there_is_a_workflow_to_check():
    assert _workflows(), f"no workflows under {WORKFLOWS}"


def test_every_action_is_pinned_to_a_commit_sha():
    loose = []
    for workflow in _workflows():
        for number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = _USES.match(line)
            if match and not _PINNED.match(match.group(1)):
                loose.append(f"{workflow.name}:{number}: {match.group(1)}")
    assert not loose, (
        "third-party actions must be pinned to a full commit SHA; a tag is "
        "mutable and .coderabbit.yaml asks reviewers to flag this.\n"
        + "\n".join(loose)
    )


def test_every_workflow_declares_its_permissions():
    missing = [
        w.name
        for w in _workflows()
        if not re.search(r"^permissions:", w.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert not missing, (
        "a workflow without a permissions: block inherits the repository "
        f"default, which may be write: {missing}"
    )
