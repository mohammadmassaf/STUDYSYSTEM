"""The input schemas the host reads. A parameter the service checks itself is typed loosely - Any
for a list or an object, `str | float` for a number (D-36) - so a bad value reaches its
structured error; the schema still says what shape to send, or the host sees `{}` and guesses."""

import asyncio

from sqlalchemy import create_engine

from studysystem.server import create_server


def schemas() -> dict[str, dict]:
    server = create_server(create_engine("sqlite://"))  # listing touches no table
    return {t.name: t.input_schema["properties"] for t in asyncio.run(server.list_tools())}


def test_attempts_is_a_list_of_objects():
    attempts = schemas()["study_submit_attempt"]["attempts"]

    assert attempts["type"] == "array"
    assert attempts["items"] == {"type": "object"}


def test_retag_is_a_list_of_objects_or_nothing():
    retag = schemas()["study_confirm_topic_proposal"]["retag"]

    # anyOf, the form pydantic gives every other optional parameter (`session_id`, ...)
    assert retag["anyOf"] == [{"type": "array", "items": {"type": "object"}}, {"type": "null"}]
    assert retag["default"] is None


def test_payload_is_an_object():
    payload = schemas()["study_submit_generation"]["payload"]

    # every kind's schema is an object at the top (items, notes, topics, claims)
    assert payload["type"] == "object"


def test_no_parameter_is_a_bare_number():
    # The SDK refuses a number it cannot read before the service runs: the host gets plain text,
    # no code or fix (D-36). A number parameter keeps a string branch (`str | float`) and is
    # checked in `values`. Optional ones too: `int | None` is anyOf[integer, null].
    def types(schema: dict) -> set:
        branches = schema.get("anyOf", [schema])
        return {b.get("type") for b in branches}

    bare = {
        (tool, name)
        for tool, props in schemas().items()
        for name, schema in props.items()
        if types(schema) & {"integer", "number"} and "string" not in types(schema)
    }
    assert bare == set()
