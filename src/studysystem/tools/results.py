"""How a service call leaves a tool (D-36).

The SDK treats any exception it does not know as a crash and sends the host one generic line, so
a `StudyError` survives only as a return value: an error result whose text is the D-17 dict as
JSON, with the same dict as structured content. A success goes out the same way, unflagged.
Anything else still raises - a crash stays a crash, and its text stays on the server.

A generation task goes out the same way, with a picture block after the JSON for each page that
has one (D-17, D-49).
"""

import json
from collections.abc import Callable

from mcp.types import CallToolResult, ImageContent, TextContent

from studysystem.errors import StudyError
from studysystem.services.generation import Task


def _result(body: dict, *, is_error: bool) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body))],
        structured_content=body,
        is_error=is_error,
    )


def error_result(err: StudyError) -> CallToolResult:
    return _result(err.to_dict(), is_error=True)


def run(call: Callable[[], dict]) -> CallToolResult:
    """Every tool body goes through this: `return run(lambda: service(...))`. The lambda keeps
    `current_user()` inside the try, so `no_user` reaches the host in the same shape."""
    try:
        return _result(call(), is_error=False)
    except StudyError as err:
        return error_result(err)


def run_task(call: Callable[[], Task]) -> CallToolResult:
    """`run` for a generation task: the payload as JSON, then its pictures in order - the same
    blocks `measure` counted into `bytes_sent`."""
    try:
        task = call()
    except StudyError as err:
        return error_result(err)
    pictures = [
        ImageContent(type="image", data=p.data, mime_type=p.mime_type) for p in task.pictures
    ]
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(task.payload)), *pictures],
        structured_content=task.payload,
        is_error=False,
    )
