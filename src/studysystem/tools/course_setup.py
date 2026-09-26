"""The setup tools (D-17, D-35): courses, their assessments, weekly capacity.

Each is one line of work: resolve the user, call the service, shape the reply through `run`.
Values arrive as `str | float | None` and the service checks them, so a bad value comes back as
the D-17 error shape rather than the SDK's argument-validation text (D-36).
"""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import assessments, capacity, courses
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_add_course",
        description=(
            "Add a course for a semester. Creates its default 'Final exam' worth 100% of the grade "
            "as an inferred weight, until the real breakdown is set. Credits, instructor and "
            "target grade start unknown; set them with study_set_course_input."
        ),
    )
    def study_add_course(code: str, name: str, semester_name: str) -> CallToolResult:
        return run(
            lambda: courses.add_course(engine, current_user(engine), code, name, semester_name)
        )

    @mcp.tool(
        name="study_set_course_input",
        description=(
            "Set one known fact about a course, found by its code. field: 'credits' (a number "
            "above 0), 'instructor' (text) or 'target_grade' (0-100, percent of the course "
            "grade). Setting a value marks it declared; it cannot be set back to unknown. Pass "
            "semester_name only when the code exists in more than one semester."
        ),
    )
    def study_set_course_input(
        code: str, field: str, value: str | float | None, semester_name: str | None = None
    ) -> CallToolResult:
        return run(
            lambda: courses.set_course_input(
                engine, current_user(engine), code, field, value, semester_name
            )
        )

    @mcp.tool(
        name="study_add_assessment",
        description=(
            "Add a graded component to a course, such as a midterm, project or lab. kind: "
            "'exam', 'project' or 'lab'. weight is the percent of the course grade; leave it "
            "out if not known yet. Does not change other assessments' weights - set those with "
            "study_set_assessment_input."
        ),
    )
    def study_add_assessment(
        code: str,
        name: str,
        kind: str,
        weight: str | float | None = None,
        semester_name: str | None = None,
    ) -> CallToolResult:
        return run(
            lambda: assessments.add_assessment(
                engine, current_user(engine), code, name, kind, weight, semester_name
            )
        )

    @mcp.tool(
        name="study_set_assessment_input",
        description=(
            "Set one known fact about a course's assessment, found by course code and "
            "assessment name (e.g. 'Final exam'). field: 'weight' (0-100), 'date' (YYYY-MM-DD, "
            "or YYYY-MM when only the month is known), 'time' (HH:MM, 24-hour) or 'room'. "
            "Setting a value marks it declared; it cannot be set back to unknown."
        ),
    )
    def study_set_assessment_input(
        code: str,
        assessment: str,
        field: str,
        value: str | float | None,
        semester_name: str | None = None,
    ) -> CallToolResult:
        return run(
            lambda: assessments.set_assessment_input(
                engine, current_user(engine), code, assessment, field, value, semester_name
            )
        )

    @mcp.tool(
        name="study_set_capacity",
        description=(
            "Record how many hours a week are available for study, from a date on (YYYY-MM-DD). "
            "Adds a new row and never edits an old one: the latest from_date on or before a day "
            "is the capacity for that day. A vacation is two calls - its start and the return."
        ),
    )
    def study_set_capacity(hours_per_week: str | float, from_date: str) -> CallToolResult:
        return run(
            lambda: capacity.set_capacity(engine, current_user(engine), hours_per_week, from_date)
        )
