# Whetstone

Evidence-gated project improvement. Point it at a repository and it finds real,
evidence-backed issues — in the code and in the running app — then reports them,
proposes fixes, or opens a pull request, within limits you configure per issue type.

It never merges and it never deploys.

**Status:** M0's deterministic core verified end-to-end on encode/httpcore on
2026-08-12 -- `init` -> `doctor` -> `run` -> `findings` -> `report`, locally on
Windows. Full transcript in
`.superpowers/sdd/2026-08-10-whetstone-m0-plan/task-12-report.md`; the fixes
from the adversarial review of that work are in `pr5-adversarial-fixes.md`
beside it. Still outstanding before M0 is done: the Ubuntu/Windows x
3.11/3.12 CI matrix.

## Install

Nothing is published yet. To follow along:

```bash
git clone https://github.com/RitvikDayal/whetstone
cd whetstone
uv sync --all-groups
```

## Commands

All six are real.

```bash
whetstone init      # interactive setup; verifies every answer by running it
whetstone doctor    # re-verifies the config against reality
whetstone run       # find issues
whetstone findings  # list what it found
whetstone report    # write a shareable HTML report
whetstone version   # print the installed version
```

### Exit codes

| Command | 0 | non-zero |
|---|---|---|
| `doctor` | every check passed or was skipped | any check FAILed |
| `run` | at least one lens ran | **no lens ran at all**, or the config could not be loaded |
| `findings` | listed (possibly nothing) | bad `--state`, or the config could not be loaded |
| `report` | written | `--out` refused, or the config could not be loaded |

`run` exiting 0 does **not** mean nothing was found — a run that did its job
and found something is a success, and `doctor` is the gate for broken
infrastructure. But a run in which **no lens ran** exits 1: a config with no
`lenses:` key, or with every lens disabled or unavailable, examines nothing,
and "nothing was checked" must never be indistinguishable from "nothing is
wrong". Whatever could not run is printed under **Not everything was checked**,
and the same list is carried into the HTML report.

If the last run did not finish — you pressed Ctrl-C, or a lens failed partway —
`findings` and `report` both say so before showing anything. A partial run's
empty result is not a clean bill of health.

## The hygiene lens

Two mechanical checks, no model calls.

`deps` audits your project's declared dependencies with
[pip-audit](https://pypi.org/project/pip-audit/), which you install yourself
(`uv tool install pip-audit`). It audits the project, not whatever Python
environment Whetstone happens to be running in: a PEP 621 `[project]` table is
resolved from `pyproject.toml`, otherwise `requirements.txt` is read. A layout
it cannot audit — a `setup.cfg`-only project, a `pyproject.toml` with no
`[project]` table — is reported as unchecked rather than quietly swapped for
something it can audit.

`coverage` reads an existing `coverage.xml` and flags line coverage below a
floor. It never runs your test suite; you generate the file.

```yaml
lenses:
  hygiene:
    enabled: true
    only: [coverage]        # optional: restrict to named detectors
    severity_floor: high    # optional: findings below this are not recorded
    options:
      coverage_floor: 80    # default 60
```

Pack-specific settings go under `options`. Everything above it is a key the
core understands, and a typo there fails validation rather than being accepted
as a setting that does nothing.

`coverage_floor` must be a number greater than 0 and no greater than 100. `0`
is rejected because a floor nothing can fall below is the check turned off
while looking exactly like a clean project. Because `options` is pack-specific
the core cannot type it at load time, so an out-of-range, non-numeric, or
boolean value loads fine and is reported as a skip when the run executes —
coverage is not evaluated at all in that case.

`only` names detectors: `deps` and `coverage`. An entry matching neither is
reported as a skip rather than silently selecting nothing.

### Boundaries do not apply to this lens

`boundaries.include` and `boundaries.exclude` narrow the files a **file-scoped**
lens examines. The hygiene lens is **project-scoped**: both detectors read paths
they choose themselves — `coverage.xml`, your dependency manifest — so those
patterns do not narrow it. Writing `exclude: ["coverage.xml"]` will not stop a
coverage finding.

That is not left to be discovered. When boundaries are configured and a
project-scoped lens is enabled, the run records a skip line saying the patterns
did not apply. A lens declares which it is with a `scope` attribute; anything
that does not declare is treated as file-scoped.

Because nothing project-scoped reads the file list, a run made up entirely of
project-scoped lenses does not resolve files at all, and works in a directory
that is not a git repository.

## Licence

AGPL-3.0-or-later. See `LICENSE`. Contributions require the `CLA.md`.
