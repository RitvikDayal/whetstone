"""The stage contracts, loaded from JSON on disk.

CONTRACTS, NOT SUGGESTIONS. Every schema sets `additionalProperties: false` and
caps every string, so a model that invents a field or returns a megabyte of
prose is refused rather than quietly stored. `provider/claude_cli.py` validates
the payload against these itself, on top of the CLI's own `--json-schema`
enforcement -- the CLI's validation is the model's side of the claim, and a
model's self-assessment is never trusted.

THE SCHEMAS LIVE AS DATA, not as Python literals, because they are also sent to
the CLI verbatim and because a diff of a contract should read as a diff of a
contract.

TWO RULES PAID FOR ON THE PREDECESSOR PROJECT, both about `minItems`:

- **`minItems` only where empty means the stage did no work.** On a field where
  empty is a real answer -- a falsifier with nothing left to doubt -- it forces
  invented content, and a model that declines to invent produces nothing valid
  at all. The stage then fails schema validation instead of reporting an honest
  empty answer, which is strictly worse than the empty answer.
- **Optional non-array fields must accept `null`.** Models fill optionals with
  `null` rather than omitting them, so `["string", "null"]` is the type of an
  optional string in practice.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent

SCHEMA_NAMES: tuple[str, ...] = ("hunt", "reproduce", "falsify", "implement")


@cache
def _read(stage: str) -> str:
    return (_HERE / f"{stage}.json").read_text(encoding="utf-8")


def load_schema(stage: str) -> dict[str, Any]:
    """The schema for *stage*, refusing an unknown one.

    Returns a fresh dict each call. The parsed schema is handed to
    `jsonschema.validate` and serialised into an argv, and a shared mutable
    dict that two stages can edit is the kind of coupling that produces a
    defect nobody can locate.
    """
    if stage not in SCHEMA_NAMES:
        raise KeyError(
            f"no schema for stage {stage!r}. Known stages: {', '.join(SCHEMA_NAMES)}."
        )
    return json.loads(_read(stage))
