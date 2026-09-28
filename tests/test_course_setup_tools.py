"""The five setup tools through a real in-process client (D-35, D-36) - what the host sees: the
names it can call, a success as JSON, and a refusal as an error result carrying the D-17 dict."""

import asyncio
import json

import pytest
from mcp import Client

from studysystem.server import create_server
from tests._papers import pdf_bytes, write

SEM = "Semester 1 2026-2027"
SETUP_TOOLS = {
    "study_add_course",
    "study_set_course_input",
    "study_add_assessment",
    "study_set_assessment_input",
    "study_set_capacity",
}


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


def test_an_exam_window_date_goes_in_approximate_through_the_tool(call):
    """D-41, the way the 16 real sittings go in: the window's first day, marked a guess."""
    add_web(call)
    call("study_add_assessment", {"code": "I3302-E", "name": "Midterm", "kind": "exam"})

    result = call(
        "study_set_assessment_input",
        {"code": "I3302-E", "assessment": "Midterm", "field": "date", "value": "~2026-11-16"},
    )

    assert not result.is_error, result.content[0].text
    assert (body(result)["value"], body(result)["date_approx"]) == ("2026-11-16", 1)


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


# --- study_add_past_exam ------------------------------------------------------


def test_add_past_exam_is_listed(service_engine):
    async def go():
        async with Client(create_server(service_engine)) as client:
            return await client.list_tools()

    assert "study_add_past_exam" in {t.name for t in asyncio.run(go()).tools}


def test_a_paper_goes_in_through_the_tool(call, tmp_path):
    add_web(call)
    path = write(tmp_path / "dl", "I3302_20192020_First.pdf", pdf_bytes("Final 2020-02-17"))

    result = call(
        "study_add_past_exam",
        {
            "code": "I3302-E",
            "assessment": "Final exam",
            "session_type": "first",
            "session_date": "2020-02-17",
            "paths": [path],
            "instructor": "Dr. Mohamad Hamze",
        },
    )

    assert not result.is_error, result.content[0].text
    assert body(result)["has_text_layer"] is True


def test_a_near_miss_slot_comes_back_as_not_found_through_the_tool(call, tmp_path):
    add_web(call)
    path = write(tmp_path / "dl", "p.pdf", pdf_bytes("x"))

    result = call(
        "study_add_past_exam",
        {
            "code": "I3302-E",
            "assessment": "Final",
            "session_type": "first",
            "session_date": "2020-02-17",
            "paths": path,
        },
    )

    assert result.is_error
    assert body(result)["code"] == "not_found"
