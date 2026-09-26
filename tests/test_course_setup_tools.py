"""The five setup tools through a real in-process client (D-35, D-36) - what the host sees: the
names it can call, a success as JSON, and a refusal as an error result carrying the D-17 dict."""

import asyncio
import json

import pytest
from mcp import Client

from studysystem.server import create_server

SEM = "Semester 1 2026-2027"
SETUP_TOOLS = {
    "study_add_course",
    "study_set_course_input",
    "study_add_assessment",
    "study_set_assessment_input",
    "study_set_capacity",
}


@pytest.fixture
def call(service_engine, user_id):
    """Call one tool on a server wired to this test's database; returns the CallToolResult.
    `user_id` is asked for only so the user row exists - every tool resolves it with
    `current_user`, as `study migrate` would have made it."""
    server = create_server(service_engine)

    def _call(name: str, args: dict):
        async def go():
            async with Client(server) as client:
                return await client.call_tool(name, args)

        return asyncio.run(go())

    return _call


def body(result) -> dict:
    return json.loads(result.content[0].text)


def add_web(call):
    return call(
        "study_add_course",
        {"code": "I3302-E", "name": "Server-Side Web Development", "semester_name": SEM},
    )


def test_the_five_setup_tools_are_listed_under_the_study_prefix(service_engine):
    async def go():
        async with Client(create_server(service_engine)) as client:
            return await client.list_tools()

    names = {t.name for t in asyncio.run(go()).tools}
    assert SETUP_TOOLS <= names
    assert all(n.startswith("study_") for n in names)


def test_a_second_add_course_is_an_error_result_with_course_exists(call):
    first = add_web(call)
    assert not first.is_error
    assert set(body(first)) == {"course_id", "slot_id", "assessment_id"}

    second = add_web(call)
    assert second.is_error
    assert body(second)["code"] == "course_exists"
    assert second.structured_content == body(second)


def test_setting_up_a_course_end_to_end(call):
    """The evening of data entry, in miniature: a course, its midterm, what is known, capacity."""
    add_web(call)
    steps = [
        ("study_set_course_input", {"code": "I3302-E", "field": "credits", "value": "4"}),
        (
            "study_add_assessment",
            {"code": "I3302-E", "name": "Midterm", "kind": "exam", "weight": 30},
        ),
        (
            "study_set_assessment_input",
            {"code": "I3302-E", "assessment": "Final exam", "field": "date", "value": "2027-01"},
        ),
        ("study_set_capacity", {"hours_per_week": 20, "from_date": "2026-10-01"}),
    ]
    for name, args in steps:
        result = call(name, args)
        assert not result.is_error, (name, result.content[0].text)

    assert body(call(*steps[2]))["date_approx"] == 1


@pytest.mark.parametrize(
    ("name", "args", "field"),
    [
        (
            "study_set_course_input",
            {"code": "I3302-E", "field": "credits", "value": "four"},
            "credits",
        ),
        (
            "study_add_assessment",
            {"code": "I3302-E", "name": "Midterm", "kind": "exam", "weight": "thirty"},
            "weight",
        ),
        (
            "study_set_capacity",
            {"hours_per_week": "lots", "from_date": "2026-10-01"},
            "hours_per_week",
        ),
    ],
)
def test_a_bad_value_comes_back_in_the_d17_shape_not_the_sdk_s(call, name, args, field):
    """D-36: the service checks values, so a word where a number goes still gets code and fix."""
    add_web(call)

    result = call(name, args)

    assert result.is_error
    err = body(result)
    assert err["code"] == "invalid_value"
    assert err["field_errors"][0]["field"] == field
    assert err["fix"]
