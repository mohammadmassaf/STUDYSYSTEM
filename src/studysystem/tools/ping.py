import datetime

from mcp.server import MCPServer


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="study_ping", description="Liveness check: returns the server's UTC time.")
    def study_ping() -> str:
        return datetime.datetime.now(datetime.UTC).isoformat()
