# What changed, and why

<!-- The why matters more. If you found the defect by measuring something, put
     the measurement here. -->

## How you know it works

<!-- Whetstone will not report a finding it cannot reproduce. Same bar here.

     If you added or changed a test, say how you made it fail against the
     UNFIXED code. A test that has never failed has never been shown to measure
     anything -- this project has caught a regression test that passed against
     the bug it was written to catch, a parametrised case that tested a
     backspace instead of a backslash, and a file count that any four files
     satisfied. -->

## Checklist

- [ ] `uv run pytest -q` and `uv run ruff check src tests` pass locally
- [ ] Every new test was forced RED against the unfixed code
- [ ] Any guarantee argued in a comment has an assertion behind it in this same PR
- [ ] Nothing declines to do work without recording a reason that reaches the user
- [ ] `CHANGELOG.md` updated, if this changes behaviour
- [ ] I agree to the [CLA](../blob/main/CLA.md)
