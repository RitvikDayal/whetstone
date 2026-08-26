# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org),
and everything below 1.0 may break without a major bump.

## 0.1.0

### Added

- **`whetstone ui` — a local control plane.** Four tabs over the same store the
  CLI reads: findings (with deciding), run (with live progress), trust and cost.
  Binds `127.0.0.1`, requires a per-session token on every API call, and
  validates the `Host` header against an allowlist. It never edits
  `whetstone.yaml` and never runs your project's commands. Behind the `ui`
  extra; see [docs/control-plane.md](docs/control-plane.md).
- **One read model.** `whetstone/readmodel.py` decides every derived fact once —
  whether a finding was *killed*, whether it was graded at all, whether a run
  finished. The CLI, the HTML report and the API all render from it, so the
  three cannot come to disagree about a verdict.
- **Runs are single-flight per project**, enforced by an OS advisory lock rather
  than one held inside a process. A run started in a terminal blocks the browser
  button and vice versa. The kernel releases it when the holder dies, including
  if it is killed, so a crashed run cannot wedge the project.

### Fixed

- **`rendered-ui` spent money and recorded nothing.** It built a budget, wrapped
  its provider, and had four early returns after the drive stage had been
  charged for — none of which wrote a cost record.
- **Cost records were keyed by run alone** while carrying a `"lens"` field, so a
  second lens writing one silently overwrote the first. Now `<run>.<lens>.json`,
  with the lens name sanitised and case-folded — `Foo` and `foo` are one file on
  Windows and macOS.
- **An absent cost record no longer reads as zero.** A malformed `spent_usd` is
  named rather than silently summed as `0.0`.
- **`list_findings` had no unique tiebreaker.** `subject` is a file path and is
  routinely shared, so identical rows were ordered by whatever SQLite chose.
- **Opening a fresh database concurrently failed about half the time.**
  Switching to WAL takes an exclusive lock and SQLite returns BUSY for it
  immediately rather than invoking the busy handler, so the connection timeout
  did not cover it. The control plane's first page load opens three connections
  at once, so a new project met this on its first screen.
- **`severity` reached the terminal unescaped.** Both it and `grade` are plain
  TEXT with no CHECK constraint, so a value from another build could raise a
  markup error and destroy the listing as it printed.

### Changed

- `execute_run` takes an optional lens-agnostic `on_event` callback for progress.
- `run_doctor` takes `execute_commands=False` for surfaces that must not run
  the project's own commands. The CLI is unchanged and still verifies by
  executing.

### Known

- **The falsifier returns opposite verdicts on identical code across runs**
  ([#33](https://github.com/RitvikDayal/whetstone/issues/33)). Nine runs on one
  candidate split 3 confirmed / 6 refuted, which establishes *that* it flips and
  not how often. Read the issue before relying on a grade.
- **`budget.ceiling.usd_per_run` is enforced per lens, not per run**
  ([#43](https://github.com/RitvikDayal/whetstone/issues/43)). The run screen
  says so where the number is shown.
- **`budget.ceiling.calls_per_day` is accepted and not enforced.** Whetstone
  keeps no cross-run accounting; both model-driven lenses report this rather
  than dropping it silently.

### The rest of what 0.1.0 is

Everything above landed on top of the work this version is mostly made of: the
evidence pipeline — hunt, reproduce in a container, falsify in a separate
process, grade — running over three lenses (`hygiene`, `code-defects`,
`rendered-ui`), a queue with six dispositions, earned per-lens autonomy, GitHub
Issues and PR sinks, and a self-contained HTML report.

**There is no earlier release to compare against.** An earlier draft of this
file had 0.1.0 as a shipped version with the control plane listed under
"Unreleased" above it. No `v0.1.0` tag was ever pushed and nothing was ever
published, so that would have described a release that did not exist and
excluded a feature that is in this one. First tag, first publish, everything
in it.
