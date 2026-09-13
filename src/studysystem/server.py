from mcp.server import MCPServer
import datetime 


mcp = MCPServer("Sometest")


@mcp.tool(name = "ping",
          description = "checking out if mcp works with claude desktop")
def ping() ->str :
    """
    checking out if mcp works with claude desktop
    """
    return datetime.datetime.now(datetime.UTC).isoformat()



if __name__ == "__main__":
    mcp.run()