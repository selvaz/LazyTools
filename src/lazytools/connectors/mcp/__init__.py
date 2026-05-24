"""Model Context Protocol (MCP) integration.

MCP is a JSON-RPC protocol for exposing tools, resources, and prompts to LLM
clients. An ``MCPServer`` acts as a *tool provider* that expands into a list of
:class:`lazybridge.Tool` entries when added to ``Agent(tools=[...])``.

Quick start::

    from lazybridge import Agent
    from lazytools.connectors.mcp import MCP

    fs = MCP.stdio(
        "fs",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp/project"],
    )
    agent = Agent("claude-opus-4-7", tools=[fs])

Install with::

    pip install lazytoolkit[mcp]
"""

from __future__ import annotations

from lazytools.connectors.mcp.server import MCP, MCPServer

__all__ = ["MCP", "MCPServer"]
