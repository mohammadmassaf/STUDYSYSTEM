"""The MCP front door: build the server, wire every tool module, run the transport."""

from mcp.server import MCPServer

from studysystem.tools import ping


def create_server() -> MCPServer:
    """Build a fully wired server. Called by __main__ and by tests."""
    mcp = MCPServer("studysystem")
    ping.register(mcp)
    return mcp


def main() -> None:
    create_server().run()
