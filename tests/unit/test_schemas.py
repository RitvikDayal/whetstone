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


def _object_subschemas(node: object, path: str = "") -> list[tuple[str, dict]]:
    """Every subschema that describes an object with named properties."""
    found: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        types = node.get("type")
        types = types if isinstance(types, list) else [types]
        if "object" in types and "properties" in node:
            found.append((path or "<root>", node))
        for key, value in node.items():
            found.extend(_object_subschemas(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_object_subschemas(item, f"{path}[{index}]"))
    return found


@pytest.mark.parametrize("stage", ["hunt", "reproduce", "falsify"])
def test_every_object_forbids_extra_properties_not_only_the_root(stage):
    """NESTED objects too. Checking only the root left `hunt.findings.items`
    and `reproduce.artifact` open, so removing either one's
    `additionalProperties: false` would have kept this green while letting a
    model invent fields inside every finding."""
    open_objects = [
        path
        for path, node in _object_subschemas(load_schema(stage), stage)
        if node.get("additionalProperties") is not False
    ]
    assert not open_objects, open_objects


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
    optional string typed `"string"` is refused for the ordinary case.

    Asserts `null` is IN the type list rather than merely that a list was used.
    The looser form accepted `["object", "string"]` -- two types, no null --
    which is exactly the shape `artifact` would take if somebody widened it
    without thinking about the declining case.
    """
    schema = load_schema(stage)
    required = set(schema.get("required", ()))
    wrong = []
    for name, spec in schema["properties"].items():
        if name in required:
            continue
        types = spec.get("type")
        types = types if isinstance(types, list) else [types]
        if "array" in types:
            continue
        if "null" not in types:
            wrong.append((name, spec.get("type")))
    assert not wrong, wrong


def test_an_empty_hunt_must_say_why():
    """`findings: []` with `notes: null` validated, so a hunt that declined,
    ran out of budget or could not read the files was indistinguishable from a
    clean repository -- the exact shape this project bans everywhere else."""
    schema = load_schema("hunt")
    validator = Draft202012Validator(schema)

    assert not validator.is_valid({"findings": []})
    assert not validator.is_valid({"findings": [], "notes": None})
    assert not validator.is_valid({"findings": [], "notes": ""})
    assert validator.is_valid({"findings": [], "notes": "read 12 files, nothing found"})


def test_a_hunt_WITH_findings_still_does_not_require_notes():
    """The requirement is conditional on purpose. Findings speak for
    themselves; it is the empty answer that needs an account."""
    finding = {
        "subject": "a.py:1",
        "title": "t",
        "observation": "o",
        "root_cause_hypothesis": "h",
        "alternative_explanations": ["other"],
        "severity": "low",
        "confidence": 0.5,
    }
    assert Draft202012Validator(load_schema("hunt")).is_valid({"findings": [finding]})


def test_load_schema_refuses_an_unknown_stage():
    with pytest.raises(KeyError):
        load_schema("nosuchstage")


def test_schema_names_matches_the_files_on_disk():
    for name in SCHEMA_NAMES:
        load_schema(name)
