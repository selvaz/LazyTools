"""MCP SDK compatibility helpers shared by server surface tests."""


def connected_client(server):
    """Return an in-process client for the installed MCP SDK generation."""
    try:
        from mcp import Client
    except ImportError:
        from mcp.shared.memory import create_connected_server_and_client_session

        return create_connected_server_and_client_session(server)
    return Client(server)


def protocol_field(obj, snake: str, camel: str):
    """Read a protocol attribute under its SDK 2 or SDK 1 spelling."""
    if hasattr(obj, snake):
        return getattr(obj, snake)
    return getattr(obj, camel)
