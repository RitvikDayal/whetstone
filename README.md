# Whetstone

Evidence-gated project improvement. Point it at a repository and it finds real,
evidence-backed issues — in the code and in the running app — then reports them,
proposes fixes, or opens a pull request, within limits you configure per issue type.

It never merges and it never deploys.

**Status: pre-release, and the honest version is below under
[Known limitations](#known-limitations).** The evidence pipeline — hunt,
reproduce in a container, falsify in a separate process, grade — has been run
against real defects in real repositories, and each of the three lenses has
been driven end to end by hand at least once.

What CI shows is narrower than that, and worth stating plainly: the suite
passes on Ubuntu and Windows across Python 3.11 and 3.12, with the
container-backed reproduce and writer tests running only on the Linux legs,
where a Docker daemon exists. A green matrix is evidence that the suite passed
with those documented skips — not that all three lenses ran end to end on every
leg.

What is not settled is how *reproducible* the falsifier's judgement is. Read the
limitations before you rely on a grade.

## Install

Not published yet. To follow along:

```bash
git clone https://github.com/RitvikDayal/whetstone
cd whetstone
uv sync --all-groups
```

The browser lens needs an extra: Playwright pulls a few hundred megabytes of
Chromium, and most people never run it.

```bash
uv sync --all-groups --all-extras
uv run playwright install chromium
```

Without it the lens reports that it could not run, with the command to fix it.
It never silently finds nothing.

## Commands

All seven are real.

```bash
whetstone init      # interactive setup; verifies every answer by running it
whetstone doctor    # re-verifies the config against reality
whetstone run       # find issues
whetstone findings  # list what it found
whetstone decide    # accept, reject, defer, hand off - the decision survives re-runs
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

## The code-defects lens

Model-driven, and every claim with a physical referent is recomputed rather than
believed. A hunt stage proposes candidates along several angles; the controller
executes the reproduction **itself**, inside a container, using your project's own
declared test command; a falsifier runs in a separate process, is denied the
hunter's hypothesis, and is told to kill the finding. The grade comes from what
the controller observed, never from the model's confidence in itself — which is
recorded and then deliberately never read.

It costs real money and is off at `tier: quick`.

## The rendered-ui lens

Defects that only exist when the app is running. A drive stage reads your markup
and proposes pairs of elements worth measuring; the controller renders the page
at each declared viewport, measures both bounding boxes, and computes the
intersection. A model saying two things overlap is a proposal. Two measured
rectangles that intersect is evidence.

Everything is measured twice, in separate browser contexts. Animations, web fonts
and async render make a single measurement a coin flip, so anything that does not
reproduce is dropped with the reason recorded.

```yaml
lenses:
  rendered-ui:
    enabled: true
    options:
      base_url: "http://127.0.0.1:3000"   # required; the browser is pinned to it
      viewports: [[1280, 800], [390, 844]]
      min_overlap_px: 4                    # below this is rounding, not a defect
```

The browser is pinned to that origin by scheme, host and port — not by prefix —
and the origin is re-checked before every measurement, so a page that redirects
cannot be reported as evidence about your app.

## Known limitations

Stated here rather than discovered later.

- **A falsifier verdict is not reproducible.** Measured 2026-08-20: the same
  borderline candidate, on the same unchanged file, ten independent runs — the
  falsifier confirmed it 3 times and refuted it 6, with one run not reaching it.
  That is the difference between grade A and grade D on identical input. Treat a
  single grade as one opinion, not a measurement.

  Two things that measurement does **not** establish, stated because it is easy
  to read more into it than it holds. It is nine runs on **one** candidate, so
  the 3-to-6 split fixes no rate: the 95% interval on it runs from about 0.12 to
  0.65 and does not exclude a coin. And the rate that decides what any fix costs
  — how often the falsifier wrongly refutes a **genuine** defect — has never
  been measured at all. Tracked in
  [#33](https://github.com/RitvikDayal/whetstone/issues/33), which now carries
  the root-cause analysis and the measurement that has to come before a fix.
- **Reads are not sandboxed.** The container bounds the reproduction, not the
  analysis stages, and the target repository's own `CLAUDE.md` is discovered into
  every stage. Point it at code you trust.
- **No estimator.** Cost is recorded per stage after the fact; nothing predicts a
  run's spend before you start it. Set `budget.ceiling.usd_per_run`.
- **`usd_per_run` is enforced per lens, not per run**, despite the name. Each
  model-driven lens holds its own budget, so with both enabled a run can spend
  twice the ceiling you set. Each still stops and reports at its own limit; it
  is the total that is unbounded.
  [#43](https://github.com/RitvikDayal/whetstone/issues/43).

## Licence

AGPL-3.0-or-later. See `LICENSE`. Contributions require the `CLA.md`.
