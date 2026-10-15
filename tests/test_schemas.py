"""The JSON Schemas the host and the dashboard read (D-11, D-54, D-163): each is a well-formed
Draft 2020-12 schema, and every keyword in it is one the validator knows. `check_schema` alone
ignores an unknown keyword, so a typo like `minimun` would pass it and the rule would silently do
nothing."""

import pytest
from jsonschema import Draft202012Validator

from studysystem.services import generation, state

# The three outbound schemas, the six inbound pieces cut from them (D-54), then /state's.
SCHEMAS = {
    "transcription": generation.TRANSCRIPTION_SCHEMA,
    "transcription item": generation.ITEM_SCHEMA,
    "transcription envelope": generation.ENVELOPE_SCHEMA,
    "note": generation.NOTE_SCHEMA,
    "note entry": generation.NOTE_ENTRY_SCHEMA,
    "note envelope": generation._NOTE_ENVELOPE_CHECK.schema,  # inline in its validator only
    "tagging": generation.TAGGING_SCHEMA,
    "tagging entry": generation.TAGGING_ENTRY_SCHEMA,
    "tagging envelope": generation._TAGGING_ENVELOPE_CHECK.schema,  # inline in its validator only
    # GET /state's sections (D-163): the dashboard reads them, so a typo there is as silent.
    **{f"state {name}": schema for name, (_, schema) in state.SECTIONS.items()},
}

# What a keyword may be: one the validator checks, or one of 2020-12's other keywords - those
# the validator reads through another (then/else via if, $defs via $ref) or carries as annotation.
KNOWN = set(Draft202012Validator.VALIDATORS) | {
    "$schema",
    "$id",
    "$anchor",
    "$dynamicAnchor",
    "$vocabulary",
    "$defs",
    "$comment",
    "then",
    "else",
    "title",
    "description",
    "default",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
    "contentMediaType",
    "contentEncoding",
    "contentSchema",
}
# Where a subschema can sit in 2020-12: one schema, a list of them, or a map of name -> schema.
ONE = (
    "items",
    "additionalProperties",
    "not",
    "if",
    "then",
    "else",
    "contains",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contentSchema",
)
LIST = ("oneOf", "anyOf", "allOf", "prefixItems")
MAP = ("properties", "patternProperties", "$defs", "dependentSchemas")


def unknown_keywords(schema, where="#") -> list[str]:
    """Every key in a schema position that is not a known keyword, with where it sits."""
    if not isinstance(schema, dict):
        return []
    found = [f"{where}/{key}" for key in schema if key not in KNOWN]
    for key in ONE:
        found += unknown_keywords(schema.get(key), f"{where}/{key}")
    for key in LIST:
        for n, sub in enumerate(schema.get(key, [])):
            found += unknown_keywords(sub, f"{where}/{key}/{n}")
    for key in MAP:
        for name, sub in schema.get(key, {}).items():
            found += unknown_keywords(sub, f"{where}/{key}/{name}")
    return found


@pytest.mark.parametrize("name", list(SCHEMAS))
def test_each_schema_is_a_well_formed_draft_2020_12_schema(name):
    Draft202012Validator.check_schema(SCHEMAS[name])


@pytest.mark.parametrize("name", list(SCHEMAS))
def test_each_schema_uses_only_known_keywords(name):
    assert unknown_keywords(SCHEMAS[name]) == []


@pytest.mark.parametrize(
    ("schema", "where"),
    [
        ({"properties": {"a": {"minimun": 1}}}, "#/properties/a/minimun"),
        ({"items": {"oneOf": [{}, {"minLenght": 1}]}}, "#/items/oneOf/1/minLenght"),
        ({"prefixItems": [{"minimun": 1}]}, "#/prefixItems/0/minimun"),
        ({"$defs": {"t": {"descripton": "x"}}}, "#/$defs/t/descripton"),
        ({"if": {}, "then": {"requird": ["a"]}}, "#/then/requird"),
    ],
)
def test_a_misspelt_keyword_is_caught_wherever_it_sits(schema, where):
    assert unknown_keywords(schema) == [where]


def test_the_known_keywords_pass_the_walk():
    """No false alarm on real 2020-12 keywords the validator does not check itself."""
    schema = {"$defs": {"t": {"deprecated": True}}, "if": {}, "then": {}, "else": {}}
    assert unknown_keywords(schema) == []
