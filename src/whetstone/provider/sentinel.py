"""Did the worktree change while a stage ran?

WHY THIS EXISTS, stated for whoever wants to delete it as redundant with the
policy gate. The gate's first version mapped `PermissionSet` onto
`--allowedTools`, which auto-approves rather than bounds, so every stage held
the CLI's full default toolset. The tests were green. They asserted the flag
appeared in the argv, and it did; what nobody asserted was the effect. A
reviewer running the real binary got a read-only stage to create files, append
to README.md and run `git init`.

So the guarantee no longer rests on the CLI honouring a flag. This module reads
the filesystem after the fact and cannot be fooled by a flag whose meaning we
misread, or whose meaning changes in a later release. Every M1a stage is
read-only, so ANY mutation is a defect and gets surfaced rather than repaired.

CONTENT, NOT STATUS LETTERS. The first version recorded `git status --porcelain`
output and nothing else, and a reviewer proved it missed seven of eight mutation
classes -- including **appending to an already-dirty tracked file**, which is
the original attack verbatim. ` M README.md` reads identically before and after,
and a working tree is normally dirty mid-run. Every path git lists is now
hashed, so the letter and the bytes both have to match.

WHAT IT DOES NOT SEE, so nobody mistakes it for total. This list is the honest
one; the first version disclosed two items and missed the five that mattered.

- **A write followed by a delete.** Undetectable by any before/after comparison,
  by construction. Not a gap that can be closed here.
- **Objects and the index inside `.git/`.** The execution surface is covered --
  `config`, `hooks/`, and every ref -- because those are what turn a repository
  into a program. A loose object nothing references is inert, and hashing every
  object would cost more than the stage.
- **Ignored files, by content.** They are listed and stat-ed, so an appearance
  or a size change is caught; a same-size same-mtime rewrite inside ignored
  space is not. Hashing `node_modules` per stage is not worth it.
- **Reads.** A stage that exfiltrates is not a mutation and this says nothing
  about it. `read_denied` is the nominal control and reaches no CLI flag, so
  there is currently NO control -- see `policy/profiles.py`.
- **Anything outside *root*.**

- **Ignored files and anything past `_HASH_CAP_ENTRIES`, by content.** Both are
  listed and stat-ed, so an appearance or a size change is caught; a same-size
  same-mtime rewrite is not. The cap exists because `--untracked-files=all`
  lists every untracked file individually and Whetstone runs against
  repositories whose state it does not choose -- a large uncovered build tree
  would otherwise be read in full, twice, per stage. Measured on Whetstone's
  own checkout: 3,508 entries.

AND IT RUNS GIT AGAINST A REPOSITORY IT DOES NOT TRUST. `core.fsmonitor`,
`core.pager`, `core.hooksPath` and `core.sshCommand` all name programs git
executes, and git reads them from the inspected worktree's own `.git/config`. A
reviewer planted one and got arbitrary code execution out of `fingerprint()` --
before any model ran, with zero tokens spent. `_GIT_HARDENING` neutralises the
config keys and `_git_env` removes the whole `GIT_` environment namespace, which
is the same attack by another route: `GIT_DIR` repoints the repository,
`GIT_CONFIG_GLOBAL` reintroduces the keys `-c` just removed, and
`GIT_EXTERNAL_DIFF` names a program. **The config half is a deny-list and is
therefore incomplete**; the durable answer is a process boundary, which is not
M1a's.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

# A non-repo root is walked instead, and a walk of an arbitrary directory is
# unbounded. Stopping is better than hanging a stage; the stop is recorded in
# the fingerprint itself so it is never a silent partial answer.
_WALK_CAP = 20_000

# Content hashing has to stop somewhere. A file over this is recorded by size
# and mtime alone, and says so in the fingerprint rather than looking hashed.
_HASH_CAP_BYTES = 8 * 1024 * 1024

_GIT_TIMEOUT = 60

# Config that makes `git` run somebody else's program, forced off. Passed with
# `-c` so it overrides the inspected repository's own `.git/config`, which is
# the file an attacker controls.
#
# `core.quotepath=false` is here for a second reason: git escapes non-ASCII
# paths to ASCII octal by default, so `cafe-<accented>.txt` reaches the user as
# `"caf\303\251.txt"` -- and the `errors="surrogateescape"` on this call is
# then defending against a hazard it never sees.
_GIT_HARDENING = [
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.sshCommand=",
    "-c", "core.editor=",
    "-c", "diff.external=",
    "-c", "core.alternateRefsCommand=",
    "-c", "protocol.ext.allow=never",
    "-c", "core.quotepath=false",
]

# The parts of `.git/` that decide whether the repository is also a program.
_GIT_EXECUTION_SURFACE = ("config", "hooks")

# Content hashing is bounded in git mode as well as in walk mode. A checkout
# with a large untracked build tree that `.gitignore` does not cover would
# otherwise be read in full, twice, on every stage -- and Whetstone runs against
# repositories whose state it does not choose. Beyond the cap an entry is still
# LISTED with a stat mark, so the cap bounds the reading and never the detection.
_HASH_CAP_ENTRIES = 2_000


def _git_env() -> dict[str, str]:
    """The environment `git` runs under: the whole `GIT_` namespace removed.

    `_GIT_HARDENING` forces off the CONFIG keys that make git execute a
    program. The environment does the same job by another route and was left
    inherited: `GIT_DIR` and `GIT_WORK_TREE` repoint the repository entirely,
    `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` reintroduce the very keys the
    `-c` overrides remove, and `GIT_EXTERNAL_DIFF`, `GIT_PAGER` and
    `GIT_SSH_COMMAND` each name a program.

    Stripping the whole namespace rather than a list of names is the only
    version of this that does not go stale the next time git adds one.

    It also makes the sentinel HERMETIC. With global and system config pointed
    at nowhere, a developer's `~/.gitconfig` cannot change what a fingerprint
    says -- on their machine, or on a CI runner whose image sets defaults.
    """
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(root: Path, args: list[str]) -> tuple[str | None, str]:
    """`(stdout, "")` when git answered, `(None, reason)` when it did not.

    THE REASON IS THE POINT. Collapsing every failure into None made
    `mode git-unavailable` read identically for a missing binary, a 60s
    timeout, a stale `index.lock` and a permission error -- so the one line
    that exists to prevent a silent partial answer stopped one step short of
    saying anything useful.

    A failure is still never raised. This runs on every stage, and a sentinel
    that takes the run down when git is missing is worse than no sentinel.
    """
    try:
        proc = subprocess.run(
            # `--no-optional-locks` stops `git status` refreshing `.git/index`,
            # which would otherwise make the index differ between the before and
            # after fingerprints on every single stage.
            ["git", "--no-optional-locks", *_GIT_HARDENING, *args],
            cwd=root,
            capture_output=True,
            # git writes path bytes as it stored them. `text=True` alone decodes
            # with the locale codec, which under cp1252 mangles every accented
            # filename; see `scope/resolver.py`, where that shipped.
            encoding="utf-8",
            errors="surrogateescape",
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            env=_git_env(),
        )
    except FileNotFoundError:
        return None, "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return None, f"git did not answer within {_GIT_TIMEOUT}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git could not be started: {type(exc).__name__}"
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or "").split())[:200] or "no stderr"
        return None, f"git exited {proc.returncode}: {detail}"
    return proc.stdout, ""


def _stat_mark(path: Path) -> str:
    """Size and mtime, or an honest absence. Never raises."""
    try:
        stat = path.stat()
    except OSError:
        return "absent"
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _digest(path: Path) -> str:
    """A content mark for one path: its hash, or an honest reason there is none.

    Never raises. This runs over paths a stage may have just deleted or made
    unreadable, and each of those states is itself a change worth recording
    rather than an error worth propagating.
    """
    try:
        stat = path.stat()
    except OSError:
        return "absent"
    if not path.is_file():
        return f"nonfile:{stat.st_mode:o}"
    if stat.st_size > _HASH_CAP_BYTES:
        # Says `stat` rather than looking like a hash. A mark that silently
        # degrades is the shape this module exists to avoid.
        return f"stat {_stat_mark(path)}"
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "unreadable"
    return f"sha256:{digest.hexdigest()[:32]}"


def _status_records(raw: str) -> list[tuple[str, str]]:
    """`(status letters, path)` from `--porcelain=v1 -z` output.

    NUL-DELIMITED, because the newline form C-quotes any path containing a
    quote, a backslash or a control character -- and stripping surrounding
    quotes does not decode those escapes, so `_digest` was handed a path that
    does not exist and returned `absent` for a file that was right there. A
    path is attacker-chosen input; a repository can carry one deliberately.

    Rename and copy entries occupy TWO records, destination first. The source
    path is consumed and dropped: the destination is the one whose content
    matters, and the `R`/`C` letters stay in the fingerprint either way.
    """
    fields = raw.split("\0")
    out: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        letters, path = entry[:2], entry[3:]
        if "R" in letters or "C" in letters:
            index += 1  # the source-path record
        out.append((letters, path))
    return out


def _surface_of(git_dir: Path) -> list[str]:
    """Config and hooks under a resolved git directory, hashed."""
    entries: list[str] = []
    for name in _GIT_EXECUTION_SURFACE:
        target = git_dir / name
        if target.is_dir():
            for child in sorted(target.rglob("*")):
                if child.is_file():
                    rel = child.relative_to(git_dir).as_posix()
                    entries.append(f".git/{rel} {_digest(child)}")
        elif target.exists():
            entries.append(f".git/{name} {_digest(target)}")
    return entries


def _git_surface(root: Path, common_dir: str | None) -> list[str]:
    """The parts of the git directory that decide whether the repository is
    also a program.

    `git status` never reports anything under `.git/`, so writing
    `.git/hooks/post-checkout` or an aliased `.git/config` was completely
    invisible -- while `.git/config` is simultaneously the file that makes git
    execute an arbitrary program. Both halves of that are proven.

    RESOLVED THROUGH GIT, not by joining `.git` onto the root. In a linked
    worktree `.git` is a FILE holding a pointer, so `is_dir()` was False and
    this returned nothing at all -- no config, no hooks, no refs -- for exactly
    the layout Whetstone will use once it starts working in worktrees.
    """
    entries: list[str] = []
    if common_dir is None:
        entries.append("git-surface unreadable (could not resolve --git-common-dir)")
    else:
        resolved = Path(common_dir)
        if not resolved.is_absolute():
            resolved = root / resolved
        entries.extend(_surface_of(resolved))
    refs, why = _git(root, ["for-each-ref", "--format=%(refname) %(objectname)"])
    if refs is None:
        entries.append(f"refs unreadable ({why})")
    else:
        entries.extend(f"ref {line}" for line in sorted(refs.splitlines()) if line)
    return entries


def _walk(root: Path) -> list[str]:
    """Every file under *root* as `relpath size:mtime_ns`, sorted, then capped.

    SORTED GLOBALLY, not per-directory. `os.walk` is depth-first, so sorting
    inside the loop gives a deterministic order that is not a lexicographic one
    -- and the cap then decides membership by traversal position. Adding one
    file could change WHICH entries survive truncation and report a large
    spurious mutation. Sorting first makes the surviving set the
    lexicographically-first `_WALK_CAP`, which changes only when those entries
    change.

    That is also why the sort happens here rather than being left to the
    filesystem: NTFS returns directory entries in name order and ext4 returns
    them in hash order, so an unsorted walk is accidentally fine on the Windows
    legs and wrong on the Ubuntu ones.

    `.git` IS PRUNED. This path is reached whenever git could not answer, which
    includes a timeout or a stale `index.lock` INSIDE a real repository -- not
    only the no-repository case the module docstring used to describe. The
    index, `logs/HEAD`, `FETCH_HEAD` and loose objects all move on their own,
    so walking them made the before and after fingerprints differ and the
    provider report `the stage is read-only and the worktree changed`. That is
    a false accusation against the model. The execution surface is picked up
    separately below, and neither `config` nor `hooks` churns.

    The TOTAL count is recorded whenever the cap bites. Without it, a file
    added past the cap is invisible -- a silent hole in exactly the case the
    cap exists to bound.
    """
    entries: list[str] = []

    def _unreadable(exc: OSError) -> None:
        # Formatted HERE rather than by `str()` afterwards. An OSError
        # stringifies with an absolute path and platform-specific text, so
        # those entries read differently from every relative entry around them
        # and differ between the Windows and Ubuntu legs.
        target = getattr(exc, "filename", None)
        try:
            shown = Path(target).relative_to(root).as_posix() if target else "?"
        except (TypeError, ValueError):
            shown = "?"
        entries.append(f"unreadable-dir {shown}")

    for parent, dirs, names in os.walk(root, onerror=_unreadable):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in names:
            path = Path(parent) / name
            entries.append(f"{path.relative_to(root).as_posix()} {_stat_mark(path)}")

    entries.extend(_surface_of(root / ".git"))
    entries.sort()
    if len(entries) > _WALK_CAP:
        return [f"... {len(entries)} entries, first {_WALK_CAP} shown", *entries[:_WALK_CAP]]
    return entries


def fingerprint(root: Path) -> str:
    """A canonical description of *root*'s working state.

    Returned as the text rather than as a digest, which is a deliberate
    departure from the plan's "hashed": a digest compares in one line and then
    cannot say WHAT changed, and a sentinel that reports "something happened"
    sends the reader to look for it by hand. Comparison is still exact -- two
    of these are equal or they are not.

    The first line names the mode AND, when it degraded, why. A run that fell
    back from git to a walk differs from one that did not, and the reader is
    told which failure caused it.
    """
    # `-z` because the newline form C-quotes awkward paths; see
    # `_status_records`. `-- .` because `git status` otherwise reports the WHOLE
    # repository regardless of cwd, so watching `repo/sub` reported a file
    # created at `repo/outside.txt` -- the two fingerprint modes would then have
    # different scopes, which is the one property a mode-switching check must
    # not have. `--ignored` is affordable now that no stage holds a shell to
    # churn a build directory, and it buys the `.gitignore: *` case, where an
    # attacker-supplied ignore file otherwise hides every write.
    status, why = _git(
        root,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored",
            "--",
            ".",
        ],
    )
    if status is None:
        return "\n".join([f"mode git-unavailable ({why})", *_walk(root)])

    # One `rev-parse` for both directories rather than two, plus a second for
    # HEAD. This runs twice per stage, so every spawn is paid ten times over a
    # four-stage lens.
    dirs, dirs_why = _git(root, ["rev-parse", "--show-toplevel", "--git-common-dir"])
    toplevel = common_dir = None
    if dirs is not None:
        parts = [line.strip() for line in dirs.splitlines() if line.strip()]
        if len(parts) == 2:
            toplevel, common_dir = parts
    # Status paths are relative to the REPOSITORY ROOT, never to cwd. Joining
    # them onto `root` worked only when the two were the same directory and
    # silently produced `absent` for every path when watching a subdirectory --
    # the same defect `scope/resolver.py` records under `--full-name`.
    base = Path(toplevel) if toplevel else root

    head, head_why = _git(root, ["rev-parse", "HEAD"])
    if head is not None:
        head_line = f"head {head.strip()}"
    elif head_why.startswith("git exited"):
        # A non-zero exit from `rev-parse HEAD`, inside a repository git has
        # just answered about, means there is no commit yet. A legitimate state,
        # recorded as itself so a first commit during a stage shows as a change.
        head_line = "head unborn"
    else:
        # A timeout or a spawn failure is NOT unborn. Writing it as `unborn`
        # would be a claim about the world taken from a failure to look.
        head_line = f"head unreadable ({head_why})"

    records = sorted(_status_records(status))
    lines = []
    for position, (letters, relative) in enumerate(records):
        target = base / relative
        if letters == "!!" or position >= _HASH_CAP_ENTRIES:
            # Ignored paths are never hashed: `node_modules` on every stage is
            # not worth what it buys. Past the cap the same applies for the same
            # reason -- and both still carry a stat mark, so an appearance or a
            # size change is caught either way.
            mark = f"stat {_stat_mark(target)}"
        else:
            mark = _digest(target)
        lines.append(f"{letters} {relative} {mark}")

    header = ["mode git"]
    if len(records) > _HASH_CAP_ENTRIES:
        header.append(f"... {len(records)} entries, first {_HASH_CAP_ENTRIES} hashed")
    if dirs is None:
        header.append(f"git-dirs unreadable ({dirs_why})")
    header.append(head_line)

    return "\n".join([*header, *sorted(lines), *_git_surface(root, common_dir)])


def assert_unchanged(root: Path, before: str) -> str | None:
    """What changed since *before*, or None.

    The return is a sentence for a human, because the caller's job is to put it
    in front of one. A read-only stage that modified the repository is the kind
    of thing that has to be read, not counted.
    """
    after = fingerprint(root)
    if after == before:
        return None

    was = set(before.splitlines())
    now = set(after.splitlines())
    added = sorted(now - was)
    removed = sorted(was - now)
    parts = []
    if added:
        parts.append("now: " + "; ".join(added[:5]))
        if len(added) > 5:
            parts.append(f"(+{len(added) - 5} more)")
    if removed:
        parts.append("was: " + "; ".join(removed[:5]))
        if len(removed) > 5:
            parts.append(f"(+{len(removed) - 5} more)")
    return (
        f"the worktree at {root} changed while the stage ran, and every M1a "
        f"stage is read-only. " + " ".join(parts)
    )
