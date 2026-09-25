"""How a service call leaves a tool (D-36), checked through a real client - what the host sees,
not what the function returns."""

import asyncio
import json

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult

from studysystem.errors import StudyError
from studysystem.tools.results import run

ERR = StudyError(
    code="course_exists",
    message="I3302-E already exists in Semester 1 2026-2027",
    fix="use study_set_course_input to change it",
    field_errors=[{"field": "code", "problem": "taken this semester"}],
)


def call(body):
    """Register `body` as a one-off tool and call it over an in-process client."""
    mcp = MCPServer("test")

    @mcp.tool(name="study_probe")
    def study_probe() -> CallToolResult:
        return run(body)

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("study_probe", {})

    return asyncio.run(go())


def fail():
    raise ERR


def test_a_study_error_reaches_the_host_as_the_whole_dict():
    result = call(fail)
    assert result.is_error
    assert json.loads(result.content[0].text) == ERR.to_dict()
    assert result.structured_content == ERR.to_dict()


def test_a_success_passes_through_untouched():
    result = call(lambda: {"course_id": "C"})
    assert not result.is_error
    assert json.loads(result.content[0].text) == {"course_id": "C"}
    assert result.structured_content == {"course_id": "C"}


def test_a_crash_is_not_swallowed():
    with pytest.raises(ZeroDivisionError):
        run(lambda: 1 / 0)
