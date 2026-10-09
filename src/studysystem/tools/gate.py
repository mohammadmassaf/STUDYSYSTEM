"""The dogfood gate (D-21, D-113): has the system been used, as the database proves it."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import gate
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_get_gate",
        description=(
            "Check whether the study plan has really been used: returns every logged hour that "
            "matches an item a plan showed, in the plan current when the work started, and "
            "used = true if there is any. An hour logged with no topic or setup key, on an item "
            "the plan did not show, or voided, does not count. An hour appears once per plan "
            "item it matches, so do not sum minutes over the rows. Read only."
        ),
    )
    def study_get_gate() -> CallToolResult:
        return run(lambda: gate.dogfood_gate(engine, current_user(engine)))
