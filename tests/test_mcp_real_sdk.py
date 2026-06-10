"""Integration tests against the **real** MCP SDK (skipped without the
``mcp`` extra; the extra is part of ``[test]`` so CI runs these).

The official SDK's sessions are loop- and task-affine. These tests pin the
two contracts that keep real-world usage working:

1. The sync ``as_tools()`` facade (what ``Agent(tools=[MCP.stdio(...)])``
   triggers via ``build_tool_map``) must leave the session usable from a
   *different* event loop afterwards — regression test for the audit finding
   where the session was created on a throwaway ``asyncio.run`` loop and
   every later call failed with ``ClosedResourceError``.
2. The SDK context must be entered and exited in the same task, or anyio
   raises "Attempted to exit cancel scope in a different task" at close.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from lazytools.connectors.mcp import MCP

_TOY_SERVER = '''\
from mcp.server.fastmcp import FastMCP

app = FastMCP("toy")


@app.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


app.run()
'''


@pytest.fixture
def toy_server(tmp_path: Path) -> str:
    path = tmp_path / "toy_mcp_server.py"
    path.write_text(_TOY_SERVER)
    return str(path)


def test_sync_discovery_then_call_on_a_fresh_loop(toy_server: str) -> None:
    """The Agent(tools=[...]) pattern: sync discovery, then tool calls on
    whatever loop the agent run happens to use."""
    srv = MCP.stdio("toy", command=sys.executable, args=[toy_server], allow=["*"])
    try:
        tools = srv.as_tools()  # sync facade — no running loop here
        assert [t.name for t in tools] == ["toy.add"]

        result = asyncio.run(asyncio.wait_for(tools[0].run(a=2, b=3), timeout=30))
        assert "5" in str(result)

        # A second call on yet another loop must work too.
        result = asyncio.run(asyncio.wait_for(tools[0].run(a=10, b=20), timeout=30))
        assert "30" in str(result)
    finally:
        asyncio.run(srv.aclose())


async def test_async_context_manager_roundtrip(toy_server: str) -> None:
    """The fully-async pattern: connect, discover, call, and close — close
    must not raise the anyio different-task cancel-scope error."""
    async with MCP.stdio("toy", command=sys.executable, args=[toy_server], allow=["*"]) as srv:
        tools = await srv.alist_tools()
        assert [t.name for t in tools] == ["toy.add"]
        result = await asyncio.wait_for(tools[0].run(a=4, b=5), timeout=30)
        assert "9" in str(result)
