# Whetstone

Evidence-gated project improvement. Point it at a repository and it finds real,
evidence-backed issues, in the code and in the running app, then reports them,
proposes fixes, or opens a pull request within limits configured per issue type.

It never merges and it never deploys. Those commands do not exist in the codebase,
and their absence is asserted by a test.

Design and plans live in the vault, not here:
`../Uddhava/03-projects/whetstone/` (`_index.md`, `brief.md`, `_plans/`).

## Hard rules

- **Never push to `main`.** `.githooks/pre-push` refuses it. Enable the hook with
  `git config core.hooksPath .githooks`. Work goes on a branch and lands via PR.
- **CodeRabbit does not review drafts.** Open the PR as a draft, finish the work,
  then mark it ready. That is the signal to review.
- **Every CodeRabbit comment gets taken seriously.** Disagree in the thread with a
  reason; never dismiss one silently.
- **Run an adversarial review locally before marking a PR ready.** The point is to
  find what CodeRabbit would find, before it has to.
- **Merge once CodeRabbit reports no remaining issues and CI is green on all four
  legs.** Both conditions, not either. A clean review over red CI is not a pass.
  Squash merge, delete the branch.
- **Write PR descriptions with the `humanizer` skill.** Brief, plain, and
  readable. No bolded-header bullet lists, no rule-of-three, no em dashes.
- **Nothing under `src/` may invoke** `git merge`, `git push`, `gh pr merge`, or
  any deploy tool.
- **No `Co-Authored-By` trailers and no "Generated with" footers** in commits or
  PR bodies.
- **Never claim a thing works until it has been observed running.** Green tests
  are necessary, not sufficient. A skipped test is not a passing test.

## Invariants the code must preserve

- Writes stay inside the resolved worktree. `never_touch` globs are enforced in
  code, never merely requested in a prompt.
- `never_touch` is a write barrier, not an analysis filter. A finding in a
  protected path is still worth reporting.
- Secrets are `${env:VAR}` references. A literal under a secret-shaped key fails
  validation.
- Any path that declines to do work records a reason that reaches the user. A run
  that quietly checked half the surface reads as clean, which is worse than not
  running at all.
- A model's self-assessment is never trusted. Claims with a physical referent are
  recomputed from the world, not from the model's own payload.

## Stack and layout

Python 3.11+ with `uv`, pytest, ruff, SQLite, Typer, Pydantic v2, pathspec,
Jinja2. `src/` layout, tests in `tests/unit/` and `tests/integration/`.

```bash
uv sync --all-groups
uv run pytest -q
uv run ruff check src tests
```

CI runs on Ubuntu and Windows across Python 3.11 and 3.12. Windows path handling
is where this codebase is most likely to break, so a green local run on one
platform proves less than it looks like it does.

## Branch protection is not yet enforceable

GitHub refuses rulesets on a private repo on the Free plan (403: upgrade to Pro or
make the repository public). `scripts/protect-main.sh` applies the real ruleset the
moment either becomes true. Until then the pre-push hook is the only guard, and it
is client-side, so `--no-verify` defeats it.
