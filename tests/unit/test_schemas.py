"""The stage contracts.

Every schema is a CONTRACT rather than a suggestion: `additionalProperties:
false` and `maxLength` caps everywhere, so drift is a hard failure instead of a
silent one. A model that invents a field gets refused; a model that returns a
megabyte of prose gets refused.
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from whetstone.schemas import SCHEMA_NAMES, load_schema


@pytest.mark.parametrize("stage", ["hunt", "reproduce", "falsify"])
def test_each_schema_is_valid_json_schema(stage):
    Draft202012Validator.check_schema(load_schema(stage))


@pytest.mark.parametrize("stage", ["hunt", "reproduce", "falsify"])
def test_each_schema_forbids_extra_properties(stage):
    assert load_schema(stage)["additionalProperties"] is False


def test_hunt_separates_facts_from_hypotheses():
    """A finding has to carry what was SEEN apart from what it is BLAMED on.

    Collapsing them is how a plausible story about a non-bug becomes a
    finding: the observation is checkable and the hypothesis is not, and a
    reader who cannot tell them apart cannot check either.
    """
    props = load_schema("hunt")["properties"]["findings"]["items"]["properties"]
    assert "observation" in props
    assert "root_cause_hypothesis" in props
    assert "alternative_explanations" in props


def test_falsify_requires_a_counterargument_even_when_confirming():
    """A falsifier that confirms without stating the best case against it has
    not falsified anything -- it has agreed."""
    schema = load_schema("falsify")
    assert "strongest_counterargument" in schema["required"]


def _constraint_keys(node: object) -> set[str]:
    """Every schema keyword used anywhere under *node*.

    Structural rather than a substring search of the serialised JSON, which the
    plan specified and which is wrong in both directions: it fails when a
    `description` merely mentions the keyword -- this schema's does, explaining
    why it is absent -- and it would pass on a schema that spelled the keyword
    only inside a `$ref` it does not resolve.
    """
    keys: set[str] = set()
    if isinstance(node, dict):
        keys |= set(node)
        for value in node.values():
            keys |= _constraint_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _constraint_keys(item)
    return keys


def test_no_schema_requires_a_nonempty_list_where_empty_is_a_real_answer():
    """`minItems` on such a field forces invented content, and a model that
    declines then produces nothing valid at all -- so the stage fails schema
    validation rather than reporting an honest empty answer, which is strictly
    worse than the empty answer."""
    field = load_schema("falsify")["properties"]["remaining_uncertainty"]
    assert "minItems" not in _constraint_keys(field)


def test_min_items_is_used_where_empty_DOES_mean_no_work():
    """The rule is not "never use minItems" -- it is "only where empty means
    the stage did nothing". A reproduce stage with no steps did nothing, and a
    hunter that cannot name one alternative explanation has not looked."""
    reproduce = load_schema("reproduce")["properties"]["steps"]
    assert reproduce["minItems"] >= 1

    finding = load_schema("hunt")["properties"]["findings"]["items"]
    assert finding["properties"]["alternative_explanations"]["minItems"] >= 1


@pytest.mark.parametrize("stage", ["hunt", "reproduce", "falsify"])
def test_every_string_field_is_capped(stage):
    """Drift is a hard failure rather than a silent one. An uncapped string is
    a model returning a megabyte of prose into the store."""
    uncapped = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            types = node.get("type")
            types = types if isinstance(types, list) else [types]
            if "string" in types and "maxLength" not in node and "enum" not in node:
                uncapped.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(load_schema(stage), stage)
    assert not uncapped, uncapped


@pytest.mark.parametrize("stage", ["hunt", "reproduce", "falsify"])
def test_optional_non_array_fields_accept_null(stage):
    """Models fill optionals with `null` rather than omitting them, so an
    optional string typed `"string"` is refused for the ordinary case."""
    schema = load_schema(stage)
    required = set(schema.get("required", ()))
    wrong = [
        name
        for name, spec in schema["properties"].items()
        if name not in required
        and isinstance(spec.get("type"), str)
        and spec["type"] != "array"
    ]
    assert not wrong, wrong


def test_load_schema_refuses_an_unknown_stage():
    with pytest.raises(KeyError):
        load_schema("nosuchstage")


def test_schema_names_matches_the_files_on_disk():
    for name in SCHEMA_NAMES:
        load_schema(name)
