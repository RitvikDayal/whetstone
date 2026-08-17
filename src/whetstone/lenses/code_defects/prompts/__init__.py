"""The stage prompts, loaded from markdown on disk.

VERSIONED FILES, NEVER STRING LITERALS. A prompt embedded in Python cannot be
diffed, reviewed or A/B'd, and the diff is the only way anyone will notice that
the instruction given to a model changed. Same shape as `whetstone.schemas`,
deliberately: two loaders that behave differently is one more thing to remember.

Placeholders are `string.Template`'s `$name` rather than `str.format`'s braces.
A prompt is markdown that will eventually carry a JSON example, and a stray `{`
in a format string is a runtime error that shows up on the day somebody adds
one -- long after the change that caused it.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

_HERE = Path(__file__).parent

PROMPT_NAMES: tuple[str, ...] = ("hunt", "reproduce", "falsify", "implement")


@cache
def load_prompt(stage: str) -> str:
    """The prompt template for *stage*, refusing an unknown one."""
    if stage not in PROMPT_NAMES:
        raise KeyError(
            f"no prompt for stage {stage!r}. Known stages: {', '.join(PROMPT_NAMES)}."
        )
    return (_HERE / f"{stage}.md").read_text(encoding="utf-8")
