You are trying to reproduce a defect somebody else reported. You are reading
only — you have no shell, so you cannot run anything yourself.

## What was observed

$observation

## Where

$subject

## The failure scenario, if one was named

$failure_scenario

## What you are asked for

A **pytest test that PASSES while the defect is present**, and would fail once
it is fixed.

That direction is deliberate and it is the opposite of a regression test. You
are not writing the test that guards the fix; you are writing the one that
demonstrates the defect is real. So assert the broken behaviour:

```python
def test_reproduces():
    with pytest.raises(IndexError):
        add([])
```

Whetstone runs this itself, through the project's own test command, and the
exit code — not your opinion — decides whether the defect is real.

## The marker, and why it exists

Every assertion in your test must carry this exact string in its message:

    WHETSTONE-REPRO

For example:

```python
assert result == 0, "WHETSTONE-REPRO: expected the empty case to return 0"
```

The reason: if your test fails, that could mean the defect is genuinely absent,
or it could mean your test is broken. Those are completely different answers
and the exit code alone cannot tell them apart. The marker is what makes the
first one distinguishable — a failure that names it is a failure of the thing
you were checking. Without it, the run is reported as inconclusive rather than
as evidence of absence.

## If you cannot write one

Say so in `notes`, set `artifact` to null, and describe the steps you would
take. That is an honest answer and is graded as one. A fabricated test that
passes for the wrong reason is worse than no test, because it will be believed.

## Rules

- `steps` is how a human would produce the behaviour by hand. At least one.
- `expected` is what correct behaviour looks like. `actual` is what happens.
- `artifact.kind` must be `pytest`. Scripts and shell commands are refused and
  the finding is capped, because Whetstone will not execute an arbitrary
  command a model wrote.
- Your test must be self-contained: it may import from the project, and it must
  not need network, fixtures, or files that do not exist.
- `reproduced` is your claim. It is overwritten by what the test actually does.
