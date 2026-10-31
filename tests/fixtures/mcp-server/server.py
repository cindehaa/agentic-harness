from mcp.server.mcpserver import MCPServer

server = MCPServer("fixture")


@server.tool()
def secret_word() -> str:
    return "pomegranate-42"  # a word no model would guess: seeing it proves the tool was called


server.run(transport="streamable-http", host="127.0.0.1", port=8000)
