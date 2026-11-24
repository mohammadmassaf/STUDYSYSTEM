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
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import uvicorn
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from studysystem.db.migrate import current_revision
from studysystem.errors import StudyError, busy
from studysystem.services import capacity, feedback, state, study_loop, trials, values
from studysystem.services.users import current_user
from studysystem.startup import call_check, open_database

HOST = "127.0.0.1"
PAGE = Path(__file__).with_name("dashboard.html")
OWN_HOSTS = {"127.0.0.1", "localhost"}

# Any code not listed is the caller's to fix, so 400.
STATUS = {
    "not_found": 404,
    "wrong_host": 403,
    "not_json": 415,
    "no_user": 500,
    "too_many_users": 500,
    "database_busy": 503,
    "database_missing": 503,
    "database_unavailable": 503,
    "schema_behind_head": 503,
    "schema_ahead_of_head": 503,
    "session_ended": 409,
    "session_expired": 409,
}

# These three answer on any schema (D-338): the page shows why the data routes refuse,
# `launcher.answering` reads /health to know the dashboard is up, and an upgrade must be able to
# stop it (D-342).
UNCHECKED_PATHS = {"/", "/health", "/shutdown"}

# How long a stop waits before it ends the process anyway (D-343). A write waits 5 s for a locked
# database, and `study stop` waits 15 s (launcher.WAIT_SECONDS).
STOP_SECONDS = 8

# Each write's service arguments, by the same names: study_log_hours's, study_start_session's,
# and the timer's stop (D-198).
HOURS_FIELDS = (
    "code",
    "minutes",
    "topic_id",
    "setup_key",
    "material_id",
    "session_id",
    "voids",
    "semester_name",
    "deviation_reason",
    "started_at",
)
START_FIELDS = ("code", "mode", "subject_type", "subject_id", "semester_name")
STOP_FIELDS = ("code", "session_id", "minutes", "setup_key", "semester_name", "deviation_reason")
ACCEPT_FIELDS = ("suggestion_id", "from_date")
CAPACITY_FIELDS = ("hours_per_week", "from_date")  # study_set_capacity's (D-290)
CONFIRM_FIELDS = ("directive_id",)  # study_confirm_directive's, from the Plan page (D-311)
TRIALS_FIELDS = ("on",)  # study_set_trials's (D-317)
# Fields the service reads as a number or a true/false; every other field must be text.
NUMBER_FIELDS = ("minutes", "hours_per_week")
FLAG_FIELDS = ("on",)


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


def _args(body: dict, route: str, fields: tuple[str, ...], needed: tuple[str, ...]) -> dict:
    """The tool's schema checks names and types before the service runs; this is that check. A
    mistyped name must not drop silently: `topic` for `topic_id` would log course-level hours,
    and `source` is no field at all - only the timer's stop writes `timer` (D-198)."""
    for field in body:
        if field not in fields:
            raise values.invalid(
                field, f"is not a field of POST {route}", f"send only: {', '.join(fields)}"
            )
    for field in needed:
        if body.get(field) is None:
            fix = "send the course code, e.g. I3302" if field == "code" else f"send {field}"
            raise values.invalid(field, "is missing", fix)
    for field in fields:
        if (
            field not in NUMBER_FIELDS + FLAG_FIELDS
            and body.get(field) is not None
            and not isinstance(body[field], str)
        ):
            raise values.invalid(field, f"{body[field]!r} is not text", f"send {field} as a string")
    return body


def _interrupt() -> None:
    """Stop this process as Ctrl+C does: uvicorn lets the running requests finish, for STOP_SECONDS
    at most, then exits."""
    signal.raise_signal(signal.SIGINT)


def create_app(
    engine: Engine,
    check: Callable[[], None] | None = None,
    stop: Callable[[], None] | None = None,
) -> Starlette:
    """The app on `engine`. Called by main(), which passes `call_check` and `_interrupt`, and by
    tests, which pass their own engine. Without `stop` there is no /shutdown."""
    version = importlib.metadata.version("studysystem")

    async def at_head(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """`check` before every data route, in one place for the reason `_own_host` gives."""
        if check is not None and request.url.path not in UNCHECKED_PATHS:
            try:
                await run_in_threadpool(check)
            except StudyError as err:
                return _error(err)
            except OperationalError as err:
                return _error(busy(err))
        return await call_next(request)

    async def page(request: Request) -> HTMLResponse:
        # The page is served here, same origin as /state and /hours, because D-162 blocks writes
        # from a file:// page (D-186).
        return HTMLResponse(await run_in_threadpool(PAGE.read_text, encoding="utf-8"))

    def revision() -> str | None:
        """What /health names. None when the file is lost or cannot be read: connecting would make
        an empty one, or fail, and the launcher must still see a dashboard here (D-340)."""
        try:
            if check is not None:
                check()
        except StudyError as err:
            if err.code in ("database_missing", "database_unavailable"):
                return None
        return current_revision(engine)

    async def health(request: Request) -> JSONResponse:
        return await run_in_threadpool(
            _answer, lambda: {"server_version": version, "schema_revision": revision()}
        )

    async def get_state(request: Request) -> JSONResponse:
        # Today in this machine's zone, as study_get_plan takes it.
        return await run_in_threadpool(
            _answer,
            lambda: state.get_state(
                engine, current_user(engine), datetime.datetime.now().astimezone().date(), version
            ),
        )

    async def shutdown(request: Request) -> JSONResponse:
        """`study stop` (D-342), which waits for the process that the reply names (D-343). A JSON
        body, as every write, so another site cannot send it."""
        try:
            await _json_body(request)
        except StudyError as err:
            return _error(err)
        stop()
        return JSONResponse({"stopping": True, "pid": os.getpid()})

    def write(
        route: str, service: Callable[..., dict], fields: tuple[str, ...], needed: tuple[str, ...]
    ) -> Route:
        """A POST route: a checked JSON body, then `service` with the current user."""

        async def handle(request: Request) -> JSONResponse:
            try:
                args = _args(await _json_body(request), route, fields, needed)
            except StudyError as err:
                return _error(err)
            # In a worker thread: a write can wait out busy_timeout, and must not stall the others.
            return await run_in_threadpool(
                _answer, lambda: service(engine, current_user(engine), **args)
            )

        return Route(route, handle, methods=["POST"])

    return Starlette(
        routes=[
            Route("/", page, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/state", get_state, methods=["GET"]),
            write("/hours", study_loop.log_hours, HOURS_FIELDS, ("code",)),
            write("/sessions", study_loop.start_session, START_FIELDS, ("code", "mode")),
            write(
                "/sessions/stop",
                study_loop.stop_timer,
                STOP_FIELDS,
                ("code", "session_id", "minutes"),
            ),
            write("/capacity", capacity.set_capacity, CAPACITY_FIELDS, CAPACITY_FIELDS),
            write(
                "/capacity/accept",
                capacity.accept_capacity_suggestion,
                ACCEPT_FIELDS,
                ACCEPT_FIELDS,
            ),
            write(
                "/directives/confirm",
                feedback.confirm_directive,
                CONFIRM_FIELDS,
                CONFIRM_FIELDS,
            ),
            write("/trials", trials.set_trials, TRIALS_FIELDS, TRIALS_FIELDS),
            *([Route("/shutdown", shutdown, methods=["POST"])] if stop else []),
        ],
        middleware=[  # in order: another site is refused before it can ask about the database
            Middleware(BaseHTTPMiddleware, dispatch=_own_host),
            Middleware(BaseHTTPMiddleware, dispatch=at_head),
        ],
    )


def main(port: int) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(message)s")
    logging.getLogger("studysystem").setLevel(logging.INFO)
    engine = open_database()
    uvicorn.run(
        create_app(engine, call_check(engine), _interrupt),
        host=HOST,
        port=port,
        timeout_graceful_shutdown=STOP_SECONDS,
    )
