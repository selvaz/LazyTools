"""Unit tests for ``lazytools.connectors.mcp``.

The official MCP SDK is NOT required for these tests — they exercise the
LazyBridge integration via :meth:`MCP.from_transport`, passing a fake
transport that implements the abstract :class:`_Transport` interface.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from lazybridge import Agent

from lazytools.connectors.mcp import MCP, MCPServer
from lazytools.connectors.mcp.transports import _Transport

# ---------------------------------------------------------------------------
# Fake transport — captures call_tool invocations; configurable tool list.
# ---------------------------------------------------------------------------


class FakeTransport(_Transport):
    def __init__(self, tools: list[dict[str, Any]] | None = None) -> None:
        # Use ``is None`` (not ``or``) so the caller can pass ``tools=[]``
        # to model an empty catalogue without falling back to the default.
        self._tools = (
            tools
            if tools is not None
            else [
                {
                    "name": "list_directory",
                    "description": "List the contents of a directory.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
                {
                    "name": "read_file",
                    "description": "Read a file from disk.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
                {
                    "name": "delete_file",
                    "description": "Delete a file from disk.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            ]
        )
        self.connected = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        self.connected = True

    async def list_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return f"result of {name}({arguments})"

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_mcpserver_is_marked_as_tool_provider() -> None:
    assert MCPServer._is_lazy_tool_provider is True


# ---------------------------------------------------------------------------
# Tool expansion + namespacing
# ---------------------------------------------------------------------------


def test_as_tools_expands_to_namespaced_tools() -> None:
    fs = MCP.from_transport("fs", FakeTransport())
    tools = fs.as_tools()
    names = [t.name for t in tools]
    assert names == ["fs.list_directory", "fs.read_file", "fs.delete_file"]


def test_namespacing_can_be_disabled() -> None:
    fs = MCP.from_transport("fs", FakeTransport(), namespace=False)
    names = [t.name for t in fs.as_tools()]
    assert names == ["list_directory", "read_file", "delete_file"]


def test_namespacing_prefix_can_be_overridden() -> None:
    fs = MCP.from_transport("fs", FakeTransport(), prefix="myfs_")
    names = [t.name for t in fs.as_tools()]
    assert names == ["myfs_list_directory", "myfs_read_file", "myfs_delete_file"]


def test_tools_carry_input_schema_from_mcp() -> None:
    fs = MCP.from_transport("fs", FakeTransport())
    [list_dir, *_] = fs.as_tools()
    d = list_dir.definition()
    assert d.parameters["type"] == "object"
    assert "path" in d.parameters["properties"]
    assert d.parameters.get("required") == ["path"]


def test_as_tools_caches_after_first_call() -> None:
    transport = FakeTransport()
    fs = MCP.from_transport("fs", transport)
    a = fs.as_tools()
    b = fs.as_tools()
    assert a is b  # cached identity


# ---------------------------------------------------------------------------
# Agent integration via build_tool_map expansion
# ---------------------------------------------------------------------------


def test_agent_tools_argument_accepts_mcp_server_directly() -> None:
    # 0.7.9 raises on unknown model strings, so use a real one (we don't
    # actually call the engine — only inspect the constructed tool map).
    agent = Agent(
        engine="claude-opus-4-7",
        tools=[MCP.from_transport("fs", FakeTransport())],
    )
    expected = {"fs.list_directory", "fs.read_file", "fs.delete_file"}
    assert expected.issubset(set(agent._tool_map.keys()))


def test_agent_tools_can_mix_mcp_with_plain_callables() -> None:
    def search(query: str) -> str:
        """Plain function used alongside an MCP server."""
        return f"hit: {query}"

    agent = Agent(
        engine="claude-opus-4-7",
        tools=[search, MCP.from_transport("fs", FakeTransport())],
    )
    assert "search" in agent._tool_map
    assert "fs.read_file" in agent._tool_map


# ---------------------------------------------------------------------------
# Calls round-trip through the transport
# ---------------------------------------------------------------------------


def test_calling_a_wrapped_tool_dispatches_through_transport() -> None:
    transport = FakeTransport()
    fs = MCP.from_transport("fs", transport)
    [list_dir, *_] = fs.as_tools()
    result = asyncio.run(list_dir.run(path="/tmp"))
    assert result == "result of list_directory({'path': '/tmp'})"
    assert transport.calls == [("list_directory", {"path": "/tmp"})]


# ---------------------------------------------------------------------------
# Allow / deny filtering
# ---------------------------------------------------------------------------


def test_allow_pattern_keeps_only_matching_tools() -> None:
    fs = MCP.from_transport(
        "fs",
        FakeTransport(),
        allow=["fs.list_*", "fs.read_*"],
    )
    names = [t.name for t in fs.as_tools()]
    assert names == ["fs.list_directory", "fs.read_file"]


def test_deny_pattern_removes_dangerous_tools() -> None:
    fs = MCP.from_transport(
        "fs",
        FakeTransport(),
        deny=["fs.delete_*"],
    )
    names = [t.name for t in fs.as_tools()]
    assert names == ["fs.list_directory", "fs.read_file"]


def test_allow_and_deny_compose_allow_then_deny() -> None:
    fs = MCP.from_transport(
        "fs",
        FakeTransport(),
        allow=["fs.*_file"],
        deny=["fs.delete_*"],
    )
    names = [t.name for t in fs.as_tools()]
    assert names == ["fs.read_file"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_lazy_connect_on_first_as_tools() -> None:
    transport = FakeTransport()
    fs = MCP.from_transport("fs", transport)
    assert transport.connected is False
    fs.as_tools()
    assert transport.connected is True


def test_async_context_manager_connects_and_closes() -> None:
    async def inner() -> tuple[bool, bool, bool]:
        transport = FakeTransport()
        fs = MCP.from_transport("fs", transport)
        async with fs:
            connected_inside = transport.connected
            assert connected_inside
        return (
            transport.connected,
            connected_inside,
            transport.closed,
        )

    _after_connect, inside, after_close = asyncio.run(inner())
    assert inside is True
    assert after_close is True


def test_closed_server_cannot_reconnect() -> None:
    async def inner() -> None:
        transport = FakeTransport()
        fs = MCP.from_transport("fs", transport)
        async with fs:
            pass
        with pytest.raises(RuntimeError, match="closed"):
            await fs.aconnect()

    asyncio.run(inner())


# ---------------------------------------------------------------------------
# MCPServer wraps tool functions with introspectable hints
# ---------------------------------------------------------------------------


def test_wrapped_func_carries_mcp_metadata() -> None:
    fs = MCP.from_transport("fs", FakeTransport())
    [list_dir, *_] = fs.as_tools()
    f = list_dir.func
    assert getattr(f, "__mcp_tool_name__", None) == "list_directory"
    assert getattr(f, "__mcp_server_name__", None) == "fs"


# ---------------------------------------------------------------------------
# Edge cases: error surfacing, empty catalogues, lazy lock.
# ---------------------------------------------------------------------------


class _RaisingTransport(_Transport):
    """Fake transport whose :meth:`connect` always fails — used to verify
    that lazy-connect errors surface at ``Agent(tools=[server])`` time
    (or at ``as_tools()`` if called directly), not at first user query."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def connect(self) -> None:
        raise self._error

    async def list_tools(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return None

    async def close(self) -> None:
        pass


def test_transport_connect_error_surfaces_at_as_tools() -> None:
    fs = MCP.from_transport("fs", _RaisingTransport(RuntimeError("subprocess failed")))
    with pytest.raises(RuntimeError, match="subprocess failed"):
        fs.as_tools()


def test_transport_connect_error_surfaces_at_agent_construction() -> None:
    """Agent(tools=[server]) calls build_tool_map → as_tools → connect.
    A failing connect should bubble up immediately."""
    fs = MCP.from_transport("fs", _RaisingTransport(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        Agent(engine="claude-opus-4-7", tools=[fs])


def test_empty_tool_catalogue_yields_no_tools() -> None:
    fs = MCP.from_transport("fs", FakeTransport(tools=[]))
    assert fs.as_tools() == []


def test_allow_filter_to_zero_tools_succeeds_silently() -> None:
    """Filtering down to nothing is a valid (if degenerate) configuration."""
    fs = MCP.from_transport(
        "fs",
        FakeTransport(),
        allow=["fs.nonexistent_*"],  # matches nothing
    )
    assert fs.as_tools() == []


def test_allow_and_deny_intersecting_to_empty_succeeds_silently() -> None:
    fs = MCP.from_transport(
        "fs",
        FakeTransport(),
        allow=["fs.*"],
        deny=["fs.*"],
    )
    assert fs.as_tools() == []


def test_lock_lazy_init_does_not_create_during_construction() -> None:
    """Constructing an MCPServer outside an event loop must not create
    an asyncio.Lock (deprecated / errors on Python ≥3.12)."""
    fs = MCP.from_transport("fs", FakeTransport())
    # The lock attribute must be present but None until first async use.
    assert fs._lock is None


def test_lock_initialised_on_first_async_use() -> None:
    fs = MCP.from_transport("fs", FakeTransport())
    asyncio.run(fs.aconnect())
    assert fs._lock is not None
    asyncio.run(fs.aclose())


# ---------------------------------------------------------------------------
# Cache TTL + invalidation (relocated from LazyBridge audit suite)
# ---------------------------------------------------------------------------


async def test_mcp_tools_cache_expires_after_ttl() -> None:
    transport = FakeTransport()
    fs = MCP.from_transport("fs", transport, cache_tools_ttl=0.05)
    first = await fs.alist_tools()
    # Second call within TTL hits cache — transport is not re-asked.
    second = await fs.alist_tools()
    assert first is second  # same cached list object

    # Wait past TTL; the next call re-fetches (rebuilt — Tool identity differs).
    await asyncio.sleep(0.1)
    third = await fs.alist_tools()
    assert third is not first
    assert [t.name for t in third] == [t.name for t in first]


async def test_mcp_invalidate_tools_cache_forces_refetch() -> None:
    transport = FakeTransport()
    fs = MCP.from_transport("fs", transport, cache_tools_ttl=600)
    first = await fs.alist_tools()
    fs.invalidate_tools_cache()
    second = await fs.alist_tools()
    assert second is not first


def test_mcp_cache_ttl_validates_value() -> None:
    with pytest.raises(ValueError, match="cache_tools_ttl"):
        MCP.from_transport("fs", FakeTransport(), cache_tools_ttl=0)


# ---------------------------------------------------------------------------
# Transport internals (relocated from LazyBridge audit suite)
# ---------------------------------------------------------------------------


def test_mcp_stdio_transport_has_per_instance_connect_lock() -> None:
    """Two StdioTransport instances get distinct locks (no shared state)."""
    from lazytools.connectors.mcp.transports import StdioTransport

    a = StdioTransport(command="echo", args=["hi"])
    b = StdioTransport(command="echo", args=["hi"])
    assert isinstance(a._connect_lock, asyncio.Lock)
    assert a._connect_lock is not b._connect_lock


def test_mcp_http_transport_has_per_instance_connect_lock() -> None:
    from lazytools.connectors.mcp.transports import HttpTransport

    t = HttpTransport(url="http://localhost:0/mcp")
    assert isinstance(t._connect_lock, asyncio.Lock)


async def test_mcp_connect_serialises_concurrent_callers() -> None:
    """Two concurrent connect() callers must not both build a stack: the SDK
    import is forced to fail, so both raise identically with no half-built state."""
    from unittest.mock import patch

    from lazytools.connectors.mcp.transports import StdioTransport

    t = StdioTransport(command="echo")
    with patch.dict("sys.modules", {"mcp": None}):
        results = await asyncio.gather(t.connect(), t.connect(), return_exceptions=True)

    assert all(isinstance(r, ImportError) for r in results)
    assert t._session is None
    assert t._stack is None


async def test_stdio_transport_list_tools_before_connect_raises_runtimeerror() -> None:
    pytest.importorskip("mcp")
    from lazytools.connectors.mcp.transports import StdioTransport

    t = StdioTransport(command="false")  # never connect
    with pytest.raises(RuntimeError, match="connect"):
        await t.list_tools()
    with pytest.raises(RuntimeError, match="connect"):
        await t.call_tool("anything", {})


async def test_http_transport_list_tools_before_connect_raises_runtimeerror() -> None:
    pytest.importorskip("mcp")
    from lazytools.connectors.mcp.transports import HttpTransport

    t = HttpTransport(url="http://127.0.0.1:1/")
    with pytest.raises(RuntimeError, match="connect"):
        await t.list_tools()
    with pytest.raises(RuntimeError, match="connect"):
        await t.call_tool("anything", {})
