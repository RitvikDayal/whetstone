"""Invariants CLAUDE.md asserts are held by a test. This is that test.

Whetstone never merges and never deploys. That is a design promise, not an
oversight, and it held only by nobody having written the code yet. Scans src/
for the commands themselves and for the argument-list spellings a subprocess
call would use.

This file names every forbidden string, so it scans src/ only and never itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

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
