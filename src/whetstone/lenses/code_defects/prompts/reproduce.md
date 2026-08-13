You are trying to reproduce a defect somebody else reported. You are reading
only — you have no shell, so you cannot run anything yourself.

## What was observed

$observation

## Where

$subject

## The failure scenario, if one was named

$failure_scenario

## What you are asked for

Write a **pytest test** that fails because of this defect and would pass once it
is fixed. Whetstone runs it for you, through this project's own test command,
and the exit code — not your opinion — decides whether the defect is real.

That has two consequences worth taking seriously:

- A test that passes for the wrong reason proves nothing and will be believed.
  Make it fail *because of the defect*, not because of a typo or a missing
  import.
- You are not being asked whether you think the defect exists. You are being
  asked for something that settles it.

If the observation does not give you enough to write such a test, say so in
`notes`, set `artifact` to null, and describe the steps you would take. That is
an honest answer and is graded as one; a fabricated test is not.

## Rules

- `steps` is how a human would produce the behaviour by hand. At least one.
- `expected` is what correct behaviour looks like. `actual` is what happens.
- `artifact.kind` must be `pytest`. Scripts and shell commands are refused and
  the finding is capped, because Whetstone will not execute arbitrary commands
  a model wrote.
- `reproduced` is your claim. It is overwritten by what the test actually does.
