# Contributing

Whetstone is a tool for finding real defects with evidence behind them. It holds
itself to the same standard, so the bar here is a little unusual and worth
reading before you spend time.

By contributing you agree to the [CLA](CLA.md), which lets the project ship
under AGPL-3.0 today and offer a commercially licensed edition later without
contacting every contributor.

## Getting set up

```bash
git clone https://github.com/RitvikDayal/whetstone
cd whetstone
uv sync --all-groups --all-extras
uv run playwright install chromium
npm --prefix src/whetstone/ui ci && npm --prefix src/whetstone/ui run build
```

```bash
uv run pytest -q
uv run ruff check src tests
```

Some tests need Docker (the reproduce and writer stages) and skip without it.
They do **not** skip on CI — see `tests/_docker.py` for why a skip there would
be a defect rather than a convenience.

## The one rule that matters

**Force every test red against the unfixed code before believing it green.**

A test that has never failed has never been shown to measure anything. This is
not a style preference here; it is the failure this project keeps finding in
itself. Real examples from its own history, all caught by doing this:

- A regression test that passed against the bug it was written to catch.
- A parametrised case written as `"a\b"` — Python reads that as a backspace, so
  the Windows path-separator case it was named for was never tested.
- A count of four files that any four files satisfied, so adding a directory
  silently stopped scanning it while the test stayed green.
- An assertion written as `X in output or Y in output`, where the second arm was
  true whether or not the thing under test worked.

If you change behaviour, break it deliberately and watch the test fail. If it
doesn't, the test is not testing your change.

### The mutation battery

For anything load-bearing, script it: apply a mutation, run the tests, restore.
`ast.parse` the mutated file first — a mutation that breaks syntax makes pytest
fail for the wrong reason, and reading that silence as evidence has happened
here more than once.

## What a good change looks like

**Comments say why, not what.** This codebase is unusually heavily commented and
the reason is that almost every comment records a defect somebody paid for.
`# increment the counter` is noise; `# NOT input_tokens: a measured call
reported 4 alongside 41,036 cache-creation tokens` is the comment that stops the
bug coming back.

**A claim in a comment needs an assertion in the same commit.** "This is safe
because X" with nothing checking X is how three separate regressions got in.

**No silent truncation.** Any path that declines to do work records a reason
that reaches the user. A check that quietly does not run is the defect this
whole project exists to prevent.

**Absence and negation are different facts.** An ungraded finding is not a
refuted one; an unmeasured cost is not a free one; no run at all is not a clean
run. If your change collapses one of those pairs, it is wrong even if the tests
pass.

**Surgical changes.** No drive-by improvements to adjacent code. Clean up only
the mess your own change made.

## Pull requests

- Branch off `main`. CI must be green on all five checks.
- **CodeRabbit reviews every PR and is the merge gate.** It is not decoration:
  it once found 21 defects, including a critical fail-open, on a branch three
  independent self-run reviewers had passed.
- Review threads must be resolved before merge. Disagreeing is fine — say why
  in the thread. A finding you decline should be declined with a reason
  recorded, not silently.
- Commit messages: imperative mood, and say **what changed and why**. If you
  found the defect by measuring something, put the measurement in the message.

## Reporting a bug

Use the issue template. The one thing that helps most is the same thing this
tool demands of itself: **something executable**. A failing command, a minimal
repository, a test that goes red. A description of what went wrong is a starting
point; a reproduction is a finding.

## Security

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## Lens packs

A lens pack that lives in its own repository and depends on Whetstone's public
plugin API is **not** a contribution to this repository and is not covered by
the CLA. That API is not published yet — it is gated on M5 — so anything built
against the current internals should expect to break.
