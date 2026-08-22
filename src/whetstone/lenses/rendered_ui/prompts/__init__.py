"""This lens's stage prompts, loaded from markdown on disk.

Versioned files, never string literals, for the reason
`lenses/code_defects/prompts` gives: a prompt embedded in Python cannot be
diffed or reviewed, and the diff is the only way anyone notices that the
instruction given to a model changed.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

_HERE = Path(__file__).parent

PROMPT_NAMES: tuple[str, ...] = ("drive",)


@cache
def load_prompt(stage: str) -> str:
    """The prompt template for *stage*, refusing an unknown one."""
    if stage not in PROMPT_NAMES:
        raise KeyError(
            f"no rendered-ui prompt for stage {stage!r}. "
            f"Known stages: {', '.join(PROMPT_NAMES)}."
        )
    return (_HERE / f"{stage}.md").read_text(encoding="utf-8")
