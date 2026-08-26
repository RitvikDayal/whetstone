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

import ast
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


# --- every write CALL is accounted for ---------------------------------------
#
# Issue #11 -- `is_write_forbidden` is check-then-write -- says in its own text
# that it "should gate M1, which is where a writer gets wired in". M1b-2 wires
# that writer in. The fix exists: `guarded_write` opens first and verifies
# through the descriptor. What did not exist is anything stopping the NEXT
# write being added beside it.
#
# PER CALL, NOT PER MODULE. The first version allowlisted whole files, so a new
# direct write added to an already-allowlisted module passed unnoticed -- an
# exemption granted for one call silently covering every future one. Each entry
# below names the exact call, so adding a second write to `reproduce.py` fails
# here until somebody says why.
#
# AST, NOT REGEX. A mode held in a variable, a call split over several lines,
# and a handle obtained from `os.fdopen` are all invisible to a pattern over
# source text -- and a module using any unrecognised form skipped the old scan
# entirely, which is the "check that quietly does not run" shape inside the
# check built to stop other checks doing that.

# {(module, spelling): (how many, why)}. The COUNT is what stops one exemption
# covering a second identical call: two `path.write_text` in one module would
# otherwise share a single entry, so the guard would pass on a write nobody
# reviewed.
_ALLOWED_WRITE_CALLS = {
    ("initialize/wizard.py", "os.open"): (1,
        "writes whetstone.yaml at a user-supplied target with its own "
        "O_NOFOLLOW open and identity check -- the barrier's job, done locally "
        "because the wizard runs before any config exists for boundaries"),
    ("initialize/wizard.py", "os.fdopen"): (1,
        "wraps the descriptor the wizard opened with O_NOFOLLOW two lines "
        "above, which is that module's local form of the same check"),
    ("budget.py", "path.write_text"): (1,
        "the cost ledger, at a path built from the run id and the LENS NAME "
        "under state_root. It moved here from lenses/code_defects/pack.py so "
        "both paying lenses write one -- rendered-ui spent money and recorded "
        "nothing -- and the lens name is sanitised against [^A-Za-z0-9._-] "
        "before it reaches the filename, because M5 opens the lens registry to "
        "third parties and a name carrying a separator would write outside the "
        "costs directory"),
    ("runlock.py", "open"): (1,
        "the run lock. It is opened to be LOCKED, not to be written -- nothing "
        "is ever written to it and a test pins it at zero bytes. Deliberately "
        "outside the barrier: guarded_write resolves boundaries from config, "
        "and the lock has to be takeable before a run has loaded anything. The "
        "path is state_root / a module-level literal, so no user or model "
        "input reaches it"),
    ("lenses/code_defects/reproduce.py", "path.write_text"): (1,
        "the pytest artifact: model-authored CONTENT, uuid4 PATH. That "
        "distinction is why it is safe, and it stops being true the moment a "
        "model names the file"),
    ("verify.py", "artifact.write_text"): (1,
        "replays the reproduction artifact; same distinction as reproduce.py, "
        "and the path is a fixed literal under the worktree"),
    ("verify.py", "path.write_bytes"): (1,
        "restores a file the verifier itself reverted, from bytes it read a "
        "moment earlier -- the path came from git status, not from a model, "
        "and refusing it would leave the worktree stripped of the fix it just "
        "approved"),
    ("lenses/rendered_ui/browser.py", "self._page.screenshot"): (1,
        "the one place Playwright is asked to put a PNG on disk. The path is "
        "built by the controller and the origin is re-checked immediately "
        "before, so a page that navigated away cannot have its image captured "
        "and reported as evidence about the app under test"),
    ("lenses/rendered_ui/capture.py", "page.screenshot"): (1,
        "the capture stage's own call into that wrapper. Allowlisted "
        "SEPARATELY on purpose: the count is what forces the next stage that "
        "wants a screenshot to be reviewed rather than inheriting this one. The "
        "path is built from a loop index and a viewport size, so no model or "
        "user input reaches it"),
    ("scope/resolver.py", "os.open"): (2,
        "the barrier itself. TWO calls, and the count says so: O_CREAT|O_EXCL "
        "first so it learns atomically whether it created the file, then a "
        "plain open for the file that already existed"),
    ("scope/resolver.py", "os.fdopen"): (1,
        "wraps the descriptor guarded_write itself opened, fstat-verified and "
        "identity-checked before anything is truncated"),
}

# Call spellings that put bytes on disk, matched on the AST rather than on text.
_WRITE_ATTRS = frozenset({
    "write_text", "write_bytes", "writelines", "touch",
    "open", "fdopen", "mkstemp", "NamedTemporaryFile",
    "copy", "copyfile", "copytree", "copy2", "move", "dump",
    # A SCREENSHOT IS A WRITE, and this scan had never heard of one. M2 added a
    # stage that puts PNGs on disk through a third-party driver, and none of the
    # spellings above appear anywhere near it -- so the whole mechanism would
    # have slipped past a guard built precisely to stop the next write being
    # added unreviewed. Adding it here is what makes the M2 constraint "a
    # screenshot is a write" true rather than merely stated.
    "screenshot",
})


def _spelling(func) -> tuple[str, str] | None:
    if isinstance(func, ast.Attribute):
        return func.attr, f"{ast.unparse(func.value)}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id, func.id
    return None


def _write_calls(tree) -> list[tuple[str, int]]:
    """Every call that could put bytes on disk, as (spelling, line).

    Deliberately narrow on the ambiguous names. `json.dumps` and
    `text.replace` are everywhere and are not writes; a false positive here
    costs an allowlist line, a false negative costs the whole guard, so the
    two are not treated as equally bad.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        named = _spelling(node.func)
        if named is None:
            continue
        attr, spelling = named
        if attr not in _WRITE_ATTRS:
            continue
        if attr == "dump" and not spelling.startswith("json."):
            continue
        if attr == "open" and spelling not in ("open", "os.open", "io.open"):
            continue
        found.append((spelling, node.lineno))
    return found


def _guarded_lines(tree) -> set[int]:
    """Line numbers inside a `with guarded_write(...)` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        if any("guarded_write" in ast.unparse(i.context_expr) for i in node.items):
            guarded.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return guarded


def _offenders_in(suffix: str, source: str) -> list[str]:
    """Unguarded, unallowlisted writes in one module.

    Extracted so the scan's own logic is testable on synthetic source. Inlined
    in the src loop, neutering it survived a mutation battery: the only thing
    that noticed was the staleness check, which is a different property.
    """
    tree = ast.parse(source)
    guarded = _guarded_lines(tree)
    offenders: list[str] = []
    seen: dict[str, int] = {}
    for spelling, line in _write_calls(tree):
        if line in guarded:
            continue
        allowed, _why = _ALLOWED_WRITE_CALLS.get((suffix, spelling), (0, ""))
        seen[spelling] = seen.get(spelling, 0) + 1
        if seen[spelling] <= allowed:
            continue
        offenders.append(f"{suffix}:{line} {spelling}")
    return offenders


def test_the_scan_reports_an_unguarded_write():
    """The scan's own logic, on synthetic source.

    Without this, replacing the allowlist check with `if True: continue` made
    the src sweep report nothing and only the staleness test noticed -- which
    is a different property and would not have gone red if the allowlist had
    also been emptied.
    """
    assert _offenders_in("made_up.py", "p.write_text('x')\n") == [
        "made_up.py:1 p.write_text"
    ]


def test_the_scan_honours_the_count_not_just_the_key():
    """One entry does not license two calls."""
    # `path.write_text` -- the exact spelling the allowlist keys on. The
    # first is covered by its entry; the second is not, and that is the
    # property.
    two = "path.write_text('a')\npath.write_text('b')\n"
    assert len(_offenders_in("lenses/code_defects/reproduce.py", two)) == 1


def test_every_write_call_in_src_is_guarded_or_allowlisted():
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    files = sorted(src.rglob("*.py"))
    assert len(files) >= 5, f"the scan is not reaching src/: {files}"

    offenders = []
    for path in files:
        offenders.extend(
            _offenders_in(
                path.relative_to(src).as_posix(),
                path.read_text(encoding="utf-8"),
            )
        )

    assert offenders == [], (
        f"{offenders} write without going through `guarded_write`. Route each "
        "through it, or add (module, spelling) to _ALLOWED_WRITE_CALLS with the "
        "reason it does not need the barrier -- checked against the call, not "
        "guessed from the filename."
    )


def test_the_write_scan_can_actually_see_a_write():
    """The counterweight. A scan matching nothing makes the guard above
    vacuous, and `offenders == []` looks identical either way."""
    found = {s for s, _ in _write_calls(ast.parse(
        "p.write_text('x')\n"
        "open(p, mode)\n"
        "shutil.copyfile(a, b)\n"
        "json.dump(o, fh)\n"
        "p.touch()\n"
        "os.fdopen(fd, 'w')\n"
    ))}
    for expected in ("p.write_text", "open", "shutil.copyfile", "json.dump",
                     "p.touch", "os.fdopen"):
        assert expected in found, f"{expected} is invisible to the scan"


def test_the_scan_does_not_flag_things_that_are_not_writes():
    """A guard that cries wolf earns an allowlist entry per false positive, and
    an allowlist nobody reads is the exemption mechanism failing open."""
    found = {s for s, _ in _write_calls(ast.parse(
        "json.dumps(o)\n"
        "p.read_text()\n"
        "conn.execute(sql)\n"
    ))}
    assert found == set(), found


def test_a_write_inside_an_unrelated_with_block_is_still_flagged():
    """`_guarded_lines` must key on `guarded_write`, not on being in a `with`.

    Treating every `with` as guarded would exempt a write inside
    `with open(...)` or `with contextlib.suppress(...)` -- and there are plenty
    of those. Proven by mutation: making the check unconditional survived until
    this test existed.
    """
    tree = ast.parse(
        "with contextlib.suppress(OSError):\n    p.write_text('x')\n"
    )
    guarded = _guarded_lines(tree)
    assert [s for s, line in _write_calls(tree) if line not in guarded] == [
        "p.write_text"
    ]


def test_a_guarded_write_needs_no_allowlist_entry():
    """The exemption that is a mechanism rather than a list."""
    tree = ast.parse(
        "with guarded_write(p, b, project_root=r) as fh:\n"
        "    fh.writelines(['x'])\n"
    )
    guarded = _guarded_lines(tree)
    assert [s for s, line in _write_calls(tree) if line not in guarded] == []


def test_every_allowlisted_call_still_exists():
    """An entry for a call that is gone is an exemption nobody removed, and the
    next write added under that spelling inherits it silently."""
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    stale = []
    for (suffix, spelling), _why in _ALLOWED_WRITE_CALLS.items():
        path = src / suffix
        if not path.exists():
            stale.append(f"{suffix} (missing)")
            continue
        found = [s for s, _ in _write_calls(ast.parse(path.read_text(encoding="utf-8")))]
        if found.count(spelling) != _ALLOWED_WRITE_CALLS[(suffix, spelling)][0]:
            stale.append(f"{suffix}:{spelling} x{found.count(spelling)}")
    assert stale == [], f"allowlisted but no longer present: {stale}"


def test_every_allowlisted_call_says_why():
    """A bare exemption is a decision nobody can review."""
    for key, (count, why) in _ALLOWED_WRITE_CALLS.items():
        assert count >= 1, key
        assert why and len(why.split()) >= 8, key
