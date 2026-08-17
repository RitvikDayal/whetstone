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


# --- every write path is accounted for ---------------------------------------
#
# Issue #11 -- `is_write_forbidden` is check-then-write -- says in its own text
# that it "should gate M1, which is where a writer gets wired in". The fix
# exists: `guarded_write` opens first and verifies through the descriptor, so
# the thing checked and the thing written are the same object. What did not
# exist is anything stopping the NEXT write path from being added beside it.
#
# M1b-2 adds an implement stage, which is the first stage permitted to write.
# This guard is what makes "every write goes through the barrier" a property
# rather than a habit: a new write in src/ fails here until it is either routed
# through `guarded_write` or allowlisted below with a reason.

# (module suffix, reason it does not need the barrier). Each was checked
# against the actual call, not assumed from the filename.
_WRITES_WITHOUT_THE_BARRIER = {
    # Writes whetstone.yaml at a user-supplied target, with its own O_NOFOLLOW
    # open and identity check -- the barrier's job, done locally because the
    # wizard runs before any config exists for `boundaries` to come from.
    "initialize/wizard.py",
    # The cost ledger, under state_root, at a path Whetstone builds from the
    # run id. No user or model input reaches it.
    "lenses/code_defects/pack.py",
    # The pytest artifact. The CONTENT is model-authored; the PATH is
    # `uuid.uuid4().hex[:12]` under the worktree, so nothing a model emits
    # reaches the filename. That distinction is the reason this is allowed, and
    # it stops being true the moment a model names the file.
    "lenses/code_defects/reproduce.py",
    # The barrier itself.
    "scope/resolver.py",
}
#
# `paths.py`, `store/db.py` and `report/html.py` are deliberately NOT here.
# The first two only `mkdir` the state root, which creates a directory rather
# than writing into a file and is not what the barrier is for; the third routes
# through `guarded_write` and is exempted by that instead. All three were in
# this list on the first draft and the staleness check below removed them,
# which is the check earning its place immediately.

_WRITE_CALL = re.compile(
    r"\.write_text\(|\.write_bytes\(|\bos\.open\(|\bopen\([^)]*['\"][wax]"
)


def test_every_write_in_src_goes_through_the_barrier_or_is_allowlisted():
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    files = sorted(src.rglob("*.py"))
    assert len(files) >= 5, f"the scan is not reaching src/: {files}"

    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if not _WRITE_CALL.search(text):
            continue
        suffix = path.relative_to(src).as_posix()
        if suffix in _WRITES_WITHOUT_THE_BARRIER:
            continue
        if "guarded_write" in text:
            continue
        offenders.append(suffix)

    assert offenders == [], (
        f"{offenders} write without going through `guarded_write`. Route the "
        "write through it, or add the module to _WRITES_WITHOUT_THE_BARRIER "
        "with the reason it does not need it -- checked against the call, not "
        "guessed from the filename."
    )


def test_the_write_scan_can_actually_see_a_write():
    """The counterweight. A regex that matches nothing makes the guard above
    vacuous, and `offenders == []` looks identical either way."""
    assert _WRITE_CALL.search("path.write_text('x')")
    assert _WRITE_CALL.search('open(p, "w")')
    assert _WRITE_CALL.search("os.open(p, os.O_WRONLY)")
    assert not _WRITE_CALL.search("path.read_text()")


def test_every_allowlisted_module_still_exists_and_still_writes():
    """An allowlist entry for a module that no longer writes is an exemption
    nobody removed, and the next writer added there inherits it silently."""
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    stale = [
        suffix
        for suffix in _WRITES_WITHOUT_THE_BARRIER
        if not (src / suffix).exists()
        or not _WRITE_CALL.search((src / suffix).read_text(encoding="utf-8"))
    ]
    assert stale == [], f"allowlisted but no longer writing: {stale}"
