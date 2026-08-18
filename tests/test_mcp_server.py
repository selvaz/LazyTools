"""Tests for the LazyTools MCP *server* (mirror of the MCP client connector).

These exercise the bridge end-to-end through a real MCP client session over
in-memory streams: provider expansion → ``list_tools`` → ``call_tool``, plus
the read-only guard and the result serializer.
"""

from __future__ import annotations

import json

import pytest
from lazybridge import Tool

from lazytools.mcp_server import (
    build_server,
    default_providers,
    expand_tools,
    result_to_text,
)
from lazytools.mcp_server.server import UNSAFE_TOOL_PATTERNS

mcp = pytest.importorskip("mcp")
from mcp.shared.memory import create_connected_server_and_client_session

# --------------------------------------------------------------------------- #
# expand_tools: isolation, read-only guard, collisions
# --------------------------------------------------------------------------- #


class _Boom:
    """A provider whose extra is 'missing' — as_tools() raises."""

    _is_lazy_tool_provider = True

    def as_tools(self):
        raise ImportError("needs some[extra]")


def _tool(name: str) -> Tool:
    def fn() -> str:
        return name

    return Tool.wrap(fn, name=name, description=name)


class _Provider:
    _is_lazy_tool_provider = True

    def __init__(self, *tools: Tool) -> None:
        self._tools = list(tools)

    def as_tools(self):
        return self._tools


def test_expand_skips_provider_that_fails_to_import():
    good = _Provider(_tool("datahub_search"))
    tool_map = expand_tools([_Boom(), good])
    assert set(tool_map) == {"datahub_search"}


def test_read_only_drops_mutating_tools():
    prov = _Provider(_tool("datahub_search"), _tool("gmail_send"), _tool("regime_fit"))
    read_only = expand_tools([prov], read_only=True)
    assert set(read_only) == {"datahub_search"}

    full = expand_tools([prov], read_only=False)
    assert set(full) == {"datahub_search", "gmail_send", "regime_fit"}


def test_unsafe_patterns_cover_known_writers():
    for name in (
        "gmail_send",
        "telegram_send_message",
        "codex_write",
        "datahub_ensure_price_history",
        "datahub_register_listing",
    ):
        assert any(p in name for p in UNSAFE_TOOL_PATTERNS)


def test_plain_callable_is_wrapped_not_skipped():
    # A bare function must be exposed under its __name__, not silently dropped
    # (Tool.wrap would require an explicit name and raise).
    def datahub_ping() -> str:
        return "pong"

    tool_map = expand_tools([datahub_ping])
    assert set(tool_map) == {"datahub_ping"}


def test_bare_tool_passed_through():
    t = _tool("datahub_search")
    tool_map = expand_tools([t])
    assert tool_map["datahub_search"] is t


def test_collision_last_wins():
    prov = _Provider(_tool("dup"))
    prov2 = _Provider(Tool.wrap(lambda: "second", name="dup", description="second"))
    tool_map = expand_tools([prov, prov2])
    assert len(tool_map) == 1


# --------------------------------------------------------------------------- #
# result_to_text serialization
# --------------------------------------------------------------------------- #


def test_result_to_text_variants():
    assert result_to_text("hello") == "hello"
    assert result_to_text(None) == "null"
    assert result_to_text(b"bytes") == "bytes"
    assert json.loads(result_to_text({"a": 1})) == {"a": 1}

    class Model:
        def model_dump_json(self) -> str:
            return '{"x": 1}'

    assert result_to_text(Model()) == '{"x": 1}'

    class Weird:
        def __str__(self) -> str:
            return "weird"

    # non-serializable object falls back to str via _json_default
    assert "weird" in result_to_text({"obj": Weird()})


# --------------------------------------------------------------------------- #
# End-to-end through a real MCP client session
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_end_to_end_list_and_call():
    from lazytools.connectors.datahub import DataHubTools
    from lazytools.testing import FakeDataHubBackend

    backend = FakeDataHubBackend()
    backend.responses["list_datasets"] = [{"id": "prices", "name": "Prices"}]

    server = build_server([DataHubTools(backend=backend)], name="lazytools-test")

    async with create_connected_server_and_client_session(server) as client:
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        assert "datahub_list_datasets" in names
        # inputSchema is passed through verbatim as a JSON-Schema object
        schema = next(t.inputSchema for t in listed.tools if t.name == "datahub_list_datasets")
        assert schema["type"] == "object"

        result = await client.call_tool("datahub_list_datasets", {})
        assert not result.isError
        payload = json.loads(result.content[0].text)
        assert payload == [{"id": "prices", "name": "Prices"}]


@pytest.mark.anyio
async def test_call_tool_error_is_reported_not_raised():
    def explode() -> str:
        raise RuntimeError("kaboom")

    prov = _Provider(Tool.wrap(explode, name="datahub_boom", description="x"))
    server = build_server([prov], name="lazytools-test")

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("datahub_boom", {})
        assert result.isError
        assert "kaboom" in result.content[0].text


# --------------------------------------------------------------------------- #
# default_providers menu
# --------------------------------------------------------------------------- #


def test_default_providers_ids_validated():
    with pytest.raises(ValueError, match="Unknown provider"):
        default_providers(["nope"])


def test_default_providers_builds_readonly_datahub():
    providers = default_providers(["datahub", "statistical"])
    # Both construct without their heavy extras; expansion is what needs them.
    assert len(providers) == 2


def test_allow_write_emits_writers_that_read_only_hides():
    # allow_write=True builds DataHubTools(allow_refresh=True), which emits the
    # ingestion writers; read_only=True must still drop them, read_only=False
    # must serve them.
    providers = default_providers(["datahub"], allow_write=True)
    served = expand_tools(providers, read_only=False)
    assert "datahub_register_listing" in served
    assert "datahub_ensure_price_history" in served

    guarded = expand_tools(providers, read_only=True)
    assert "datahub_register_listing" not in guarded
    assert "datahub_ensure_price_history" not in guarded
    # ...while the read tools survive the guard.
    assert "datahub_search" in guarded


def test_default_providers_readonly_omits_writers():
    providers = default_providers(["datahub"])  # allow_write=False
    served = expand_tools(providers, read_only=False)  # guard off, still no writers
    assert "datahub_register_listing" not in served
    assert "datahub_ensure_price_history" not in served
    assert "datahub_search" in served


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --------------------------------------------------------------------------- #
# Streamable HTTP transport
#
# Why this transport exists alongside stdio: a stdio server belongs to the
# client that spawned it, so its tool list is fixed for that client's
# lifetime, and Claude Code does not restart a stdio server that exits
# ("stdio servers are local processes and are not reconnected
# automatically"). Over HTTP the process is its own and a reconnecting client
# re-reads the tool list, so picking up a changed tool costs a restart of the
# server rather than of the editor.
# --------------------------------------------------------------------------- #
def test_http_app_mounts_the_server_without_binding_a_port():
    """`http_app` returns the ASGI app rather than running it, so the wiring
    is testable without a live port -- and so a caller can mount it inside a
    larger app."""
    from lazytools.connectors.datahub import DataHubTools
    from lazytools.mcp_server.server import http_app
    from lazytools.testing import FakeDataHubBackend

    server = build_server([DataHubTools(backend=FakeDataHubBackend())], name="lazytools-test")
    app = http_app(server, path="/mcp")

    mounted = [r for r in app.routes if getattr(r, "path", None) == "/mcp"]
    assert mounted, f"no route mounted at /mcp; got {[getattr(r, 'path', r) for r in app.routes]}"


def test_http_app_path_is_configurable():
    from lazytools.connectors.datahub import DataHubTools
    from lazytools.mcp_server.server import http_app
    from lazytools.testing import FakeDataHubBackend

    server = build_server([DataHubTools(backend=FakeDataHubBackend())], name="lazytools-test")
    app = http_app(server, path="/tools")
    assert any(getattr(r, "path", None) == "/tools" for r in app.routes)


def test_cli_accepts_http_flags_without_starting_a_server():
    """--http/--host/--port parse, and default to loopback: these tools read
    and (with --allow-unsafe) write local production databases, so a default
    reachable from the network would be the wrong one."""
    from lazytools.mcp_server.__main__ import _parse_args

    args = _parse_args(["--http"])
    assert args.http is True
    assert args.host == "127.0.0.1"
    assert args.port == 8787

    args = _parse_args(["--http", "--host", "0.0.0.0", "--port", "9999"])
    assert (args.host, args.port) == ("0.0.0.0", 9999)

    assert _parse_args([]).http is False
