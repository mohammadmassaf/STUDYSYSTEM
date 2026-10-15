"""The dashboard front door (D-17, D-19): an HTTP route beside the MCP server, in a process of its
own (`study dashboard`), so it serves and writes with no host open. It calls the services the tools
call - one set of write rules, two entrances - and an error leaves in the same D-17 dict.

No login in v1 (D-05). What stands in for one is D-162: the route listens on 127.0.0.1 only; a
write must be JSON, so a page on another site has to ask the browser's permission first and is
never given it; and every request must name our own host, so a site that re-points its name to
127.0.0.1 is refused too.
"""

import datetime
import importlib.metadata
import logging
import sys
from collections.abc import Callable

import uvicorn
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from studysystem.db.migrate import current_revision
from studysystem.errors import StudyError, busy
from studysystem.services import state, study_loop, values
from studysystem.services.users import current_user
from studysystem.startup import open_database

HOST = "127.0.0.1"
OWN_HOSTS = {"127.0.0.1", "localhost"}

# Any code not listed is the caller's to fix, so 400.
STATUS = {
    "not_found": 404,
    "wrong_host": 403,
    "not_json": 415,
    "no_user": 500,
    "too_many_users": 500,
    "database_busy": 503,
}

# study_log_hours's arguments, by the same names.
HOURS_FIELDS = ("code", "minutes", "topic_id", "setup_key", "session_id", "voids", "semester_name")


def _error(err: StudyError) -> JSONResponse:
    return JSONResponse(err.to_dict(), status_code=STATUS.get(err.code, 400))


def _answer(call: Callable[[], dict]) -> JSONResponse:
    """`tools.results.run` for HTTP: a success as JSON, a `StudyError` in the D-17 shape, a lock
    wait as `database_busy`. Anything else raises, and the server logs it."""
    try:
        return JSONResponse(call())
    except StudyError as err:
        return _error(err)
    except OperationalError as err:
        return _error(busy(err))


async def _own_host(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """D-162 point 2, before every route - so a new route cannot forget it: a page whose own name
    was re-pointed to 127.0.0.1 still sends that name."""
    name = request.headers.get("host", "").rsplit(":", 1)[0]
    if name not in OWN_HOSTS:
        return _error(
            StudyError(
                code="wrong_host",
                message=f"this route answers requests to 127.0.0.1 or localhost, not {name!r}",
                fix=f"open the dashboard at http://{HOST}:<port>/",
            )
        )
    return await call_next(request)


async def _json_body(request: Request) -> dict:
    """D-162 point 1: a cross-site JSON request is preceded by a permission check the route never
    grants, so only a same-site page can send one."""
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json":
        raise StudyError(
            code="not_json",
            message=f"a write takes a JSON body, got {media or 'no content type'!r}",
            fix="send the body as JSON with Content-Type: application/json",
        )
    try:
        body = await request.json()
    except ValueError:
        raise values.invalid("body", "is not valid JSON", "send one JSON object") from None
    if not isinstance(body, dict):
        raise values.invalid("body", "is not a JSON object", "send one JSON object")
    return body


def _hours_args(body: dict) -> dict:
    """The tool's schema checks names and types before the service runs; this is that check. A
    mistyped name must not drop silently: `topic` for `topic_id` would log course-level hours."""
    for field in body:
        if field not in HOURS_FIELDS:
            raise values.invalid(
                field, "is not a field of POST /hours", f"send only: {', '.join(HOURS_FIELDS)}"
            )
    if body.get("code") is None:
        raise values.invalid("code", "is missing", "send the course code, e.g. I3302")
    for field in HOURS_FIELDS:
        if field != "minutes" and body.get(field) is not None and not isinstance(body[field], str):
            raise values.invalid(field, f"{body[field]!r} is not text", f"send {field} as a string")
    return body


def create_app(engine: Engine) -> Starlette:
    """The app on `engine`. Called by main() and by tests, which pass their own engine."""
    version = importlib.metadata.version("studysystem")

    async def health(request: Request) -> JSONResponse:
        return await run_in_threadpool(
            _answer,
            lambda: {"server_version": version, "schema_revision": current_revision(engine)},
        )

    async def get_state(request: Request) -> JSONResponse:
        # Today in this machine's zone, as study_get_plan takes it.
        return await run_in_threadpool(
            _answer,
            lambda: state.get_state(
                engine, current_user(engine), datetime.datetime.now().astimezone().date(), version
            ),
        )

    async def hours(request: Request) -> JSONResponse:
        try:
            args = _hours_args(await _json_body(request))
        except StudyError as err:
            return _error(err)
        # In a worker thread: a write can wait out busy_timeout, and must not stall the others.
        return await run_in_threadpool(
            _answer, lambda: study_loop.log_hours(engine, current_user(engine), **args)
        )

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/state", get_state, methods=["GET"]),
            Route("/hours", hours, methods=["POST"]),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_own_host)],
    )


def main(port: int) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(message)s")
    logging.getLogger("studysystem").setLevel(logging.INFO)
    uvicorn.run(create_app(open_database()), host=HOST, port=port)
