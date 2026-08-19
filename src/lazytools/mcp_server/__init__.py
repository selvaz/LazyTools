"""LazyTools MCP **server** — expose LazyTools' tool providers over MCP.

The mirror of :mod:`lazytools.connectors.mcp` (the MCP *client*). Where the
client turns an external MCP server into ``lazybridge.Tool`` entries, this
server turns LazyTools' own providers into an MCP endpoint any host
(Claude Desktop, Claude Code, Codex, …) can call.

Quick start (stdio, the transport MCP hosts launch)::

    python -m lazytools.mcp_server            # all read-only providers
    python -m lazytools.mcp_server datahub statistical   # a subset

Claude Desktop / Claude Code config::

    {
      "mcpServers": {
        "lazytools": { "command": "lazytools-mcp" }
      }
    }

Programmatic use::

    import asyncio
    from lazytools.mcp_server import build_server, serve_stdio, default_providers

    server = build_server(default_providers())   # read-only by default
    asyncio.run(serve_stdio(server))

Install with::

    pip install "lazytoolkit[mcp] @ git+https://github.com/selvaz/LazyTools.git"

The server is **read-only by default**; see :func:`build_server` for the
safety model.
"""

from __future__ import annotations

from lazytools.mcp_server.providers import PROVIDER_FACTORIES, default_providers
from lazytools.mcp_server.server import (
    UNSAFE_TOOL_PATTERNS,
    build_server,
    expand_tools,
    result_to_text,
    serve_stdio,
)

__all__ = [
    "PROVIDER_FACTORIES",
    "UNSAFE_TOOL_PATTERNS",
    "build_server",
    "default_providers",
    "expand_tools",
    "result_to_text",
    "serve_stdio",
]
