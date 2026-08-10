# Whetstone

Evidence-gated project improvement. Point it at a repository and it finds real,
evidence-backed issues — in the code and in the running app — then reports them,
proposes fixes, or opens a pull request, within limits you configure per issue type.

It never merges and it never deploys.

**Status:** early development. M0 ships the deterministic core and a zero-cost
`hygiene` lens.

## Install

```bash
pip install whetstone-cli
```

## Use

```bash
whetstone init      # interactive setup; verifies every answer by running it
whetstone doctor    # re-verifies the config against reality
whetstone run       # find issues
whetstone findings  # list what it found
whetstone report    # write a shareable HTML report
```

## Licence

AGPL-3.0-or-later. See `LICENSE`. Contributions require the `CLA.md`.
