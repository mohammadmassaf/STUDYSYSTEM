"""Open the dashboard, and turn its Windows start on or off (D-289)."""

from mcp.server import MCPServer
from mcp.types import CallToolResult

from studysystem.services import launcher
from studysystem.tools.results import run


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        name="study_dashboard",
        description=(
            "Open the student's study dashboard in their browser: the page with the stopwatch, "
            "today's tasks, the plan and the timetable. Call it when they ask to open, show or "
            "start the dashboard or the stopwatch. action: 'open' (the default) starts the "
            "dashboard if it is not running and opens it; 'autostart_on' makes Windows start it "
            "at every login, in the background; 'autostart_off' stops that. When the reply's "
            "ask_user is not null, put that question to the student in your own words, then call "
            "this tool again with 'autostart_on' for yes or 'autostart_off' for no. Share the "
            "url in case the browser did not open."
        ),
    )
    def study_dashboard(action: str = "open") -> CallToolResult:
        return run(lambda: launcher.dashboard(action))
