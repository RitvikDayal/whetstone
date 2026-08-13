"""Prompts are versioned files, never Python string literals.

A prompt in a literal cannot be diffed, reviewed or A/B'd -- and the diff is
the only way anyone will ever see that the instruction to a model changed.
Same loader shape as `schemas`, and the same wheel check, because a prompt
missing from the package is invisible from a source checkout.
"""

from __future__ import annotations

import pytest

from whetstone.lenses.code_defects.prompts import PROMPT_NAMES, load_prompt


@pytest.mark.parametrize("stage", ["hunt", "reproduce", "falsify"])
def test_each_prompt_loads_and_says_something(stage):
    text = load_prompt(stage)
    assert text.strip()
    assert len(text) > 200, "a prompt this short is a placeholder"


def test_load_prompt_refuses_an_unknown_stage():
    with pytest.raises(KeyError, match="nosuchstage"):
        load_prompt("nosuchstage")


def test_prompt_names_matches_the_files_on_disk():
    for name in PROMPT_NAMES:
        load_prompt(name)


def test_the_hunt_prompt_carries_the_placeholders_the_code_fills():
    """`string.Template` and `$name`, not `str.format` and braces: a prompt is
    markdown that will eventually contain a JSON example, and `{` in a format
    string is a runtime error nobody sees until that day."""
    text = load_prompt("hunt")
    assert "$angle" in text
    assert "$files" in text
