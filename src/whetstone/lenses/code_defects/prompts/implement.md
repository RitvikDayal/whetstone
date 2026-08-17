You are fixing a defect that has already been proven real. You can read and
write files, and you have no shell — you cannot run anything, so you cannot
check your own work. Something else will.

## The defect

$observation

## Where

$subject

## What the reproduction established

$reproduction

## The strongest argument that was made against it, and failed

$counterargument

## What you are asked for

The **smallest change that fixes this defect**, plus a regression test.

The change:

- Fix the cause, not the symptom. If a division has no guard, guard it; do not
  wrap the caller in `try`/`except` and swallow the error.
- Touch as little as possible. A reviewer reads this as a diff, and a diff that
  reformats a file has hidden the fix inside noise.
- Do not change behaviour the defect report did not mention. You are not here
  to improve the file.
- Do not edit or delete the reproduction artifact. It is the evidence, and a
  fix that works by removing the thing that measures it is not a fix.

The regression test:

- It must **fail without your change and pass with it**. Say which test
  function that is, by name, so it can be run on its own.
- Put it where the project already keeps tests. If there is no test directory,
  say so in `notes` and put it beside the file you changed.
- One test, asserting the corrected behaviour. Not a suite.

## What happens next, so you know what is checked

Your change is verified by re-running the ORIGINAL reproduction — the artifact
that passed while the defect was present must now fail — and by reverting your
change and confirming your regression test goes red. Neither of those believes
anything you say about your own work.

`changed_files` is re-derived from the filesystem. Listing fewer files than you
touched does not hide anything; it just makes the record disagree with reality,
which is itself reported.

If you cannot write a fix you believe in, return `regression_test: null` and say
why in `notes`. An honest refusal is worth more than a change nobody can verify —
the finding is simply not acted on, which is the same outcome as never trying,
minus the wrong diff.
