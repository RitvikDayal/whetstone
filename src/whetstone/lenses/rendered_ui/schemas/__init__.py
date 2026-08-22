"""This lens's own stage contracts.

LENS-LOCAL ON PURPOSE, and it is worth saying why rather than leaving it to be
read as an oversight. `whetstone.schemas` holds the spine's four stage contracts
behind a hardcoded `SCHEMA_NAMES` tuple, so a second lens pack adding a stage
would have to edit the spine to declare it. That is a coupling M2 exists to find,
and taking it would have made the abstraction gate pass while the abstraction
quietly got worse.

Same shape as the spine loader deliberately -- refusing an unknown name rather
than falling back -- because two loaders that behave differently is one more
thing to remember.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent

SCHEMA_NAMES: tuple[str, ...] = ("drive",)


@cache
def _read(stage: str) -> str:
    return (_HERE / f"{stage}.json").read_text(encoding="utf-8")


def load_schema(stage: str) -> dict[str, Any]:
    """The schema for *stage*, refusing an unknown one.

    A fresh dict per call, for the reason the spine loader gives: the parsed
    schema is handed to a validator and serialised into an argv, and a shared
    mutable dict two stages can edit is coupling nobody can locate later.
    """
    if stage not in SCHEMA_NAMES:
        raise KeyError(
            f"no rendered-ui schema for stage {stage!r}. "
            f"Known stages: {', '.join(SCHEMA_NAMES)}."
        )
    return json.loads(_read(stage))
