"""Reach a server the way a host does: through an MCP Client, one call per connection. The one
place a test opens a Client for a single call, so an SDK change to connecting is one edit."""

import asyncio

from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult


def call_tool(server: MCPServer, name: str, args: dict) -> CallToolResult:
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args)

    return asyncio.run(go())


def tool_names(server: MCPServer) -> set[str]:
    async def go():
        async with Client(server) as client:
            return await client.list_tools()

    return {t.name for t in asyncio.run(go()).tools}
