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
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from lazytools.connectors.mcp import MCP

_TOY_SERVER = '''\
try:
    from mcp.server import MCPServer as _Server
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server

app = _Server("toy")


@app.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


app.run(transport="stdio")
'''

_ANNOTATED_SERVER = '''\
try:
    from mcp.server import MCPServer as _Server
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server

app = _Server("annotated")


@app.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def read_record() -> str:
    """Read a record."""
    return "read"


@app.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def destroy_record() -> str:
    """Destroy a record."""
    return "destroyed"


@app.tool()
def unknown_record() -> str:
    """A tool without annotations."""
    return "unknown"


app.run(transport="stdio")
'''


def test_http_custom_headers_follow_trailing_slash_redirect() -> None:
    """A header-bearing HTTP client must follow an in-origin 307.

    SDK 2.0's default client sets ``follow_redirects``. A bare ``httpx2``
    client does not, so ``MCP.http(..., headers=...)`` would stop on the
    redirect that a header-less connection follows (a trailing-slash 307).
    Later SDKs follow that redirect inside the transport; either way the
    ``Authorization`` header has to arrive at the redirected path.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from lazytools.connectors.mcp.transports import HttpTransport

    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            if self.path == "/mcp":
                host = self.headers["Host"]
                self.send_response(307)
                self.send_header("Location", f"http://{host}/mcp/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization", "")
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "stop"}}
            ).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_DELETE(self) -> None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, name="mcp-redirect", daemon=True)
    thread.start()
    try:
        transport = HttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": "Bearer secret-token"},
        )
        # The stub answers the redirected POST with a JSON-RPC error, so the
        # handshake fails. What matters is that it failed *after* the header
        # arrived on ``/mcp/``, not on the 307.
        handshake_failed = False
        try:
            asyncio.run(asyncio.wait_for(transport.connect(), timeout=15))
        except Exception:
            handshake_failed = True
        assert handshake_failed
    finally:
        server.shutdown()
        server.server_close()

    assert seen.get("path") == "/mcp/"
    assert seen.get("authorization") == "Bearer secret-token"


def test_installed_sdk_openers_construct_without_connecting() -> None:
    """Importing the SDK 1 or SDK 2 client symbols must succeed before connect.

    This is the failure mode of a pin that still names ``streamablehttp_client``
    on SDK 2, or ``mcp.Client`` on SDK 1: ``MCP.stdio`` / ``MCP.http`` would
    raise ``ImportError`` at construction of the session, not at the first
    byte on the wire.
    """
    from importlib.metadata import version

    from lazytools.connectors.mcp._sdk import mcp_major, open_http, open_stdio

    major = int(version("mcp").split(".", 1)[0])
    assert mcp_major() == major
    assert major in (1, 2)

    stdio_cm = open_stdio(sys.executable, ["-c", "pass"], None)
    http_cm = open_http("http://127.0.0.1:9/mcp", {"Authorization": "Bearer test"})
    assert hasattr(stdio_cm, "__aenter__")
    assert hasattr(http_cm, "__aenter__")


@pytest.fixture
def toy_server(tmp_path: Path) -> str:
    path = tmp_path / "toy_mcp_server.py"
    path.write_text(_TOY_SERVER)
    return str(path)


def test_stdio_discovery_preserves_tool_annotations(tmp_path: Path) -> None:
    path = tmp_path / "annotated_mcp_server.py"
    path.write_text(_ANNOTATED_SERVER, encoding="utf-8")
    srv = MCP.stdio("annotated", command=sys.executable, args=[str(path)], allow=["*"])
    try:
        tools = {tool.name: tool for tool in srv.as_tools()}
        assert tools["annotated.read_record"].annotations == {
            "readOnlyHint": True,
            "openWorldHint": False,
        }
        assert tools["annotated.destroy_record"].annotations == {
            "readOnlyHint": False,
            "destructiveHint": True,
        }
        assert tools["annotated.unknown_record"].annotations == {}
    finally:
        asyncio.run(srv.aclose())


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


def test_stdio_datahub_first_lazy_pandas_call_returns(tmp_path: Path) -> None:
    """Scientific imports are warmed on the server's main thread before calls."""
    pytest.importorskip("pandas")

    package = tmp_path / "market_data_hub"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "agent_tools.py").write_text(
        """\
import json
import sys

def tool_list_datasets():
    preimported = "pandas" in sys.modules
    import pandas
    return json.dumps({"pandas_preimported": preimported, "version": pandas.__version__})
""",
        encoding="utf-8",
    )

    env = os.environ.copy()
    source = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), source, env.get("PYTHONPATH", "")))
    srv = MCP.stdio(
        "lazytools",
        command=sys.executable,
        args=["-m", "lazytools.mcp_server", "datahub"],
        env=env,
        allow=["lazytools.datahub_list_datasets"],
    )
    try:
        tools = srv.as_tools()
        assert [tool.name for tool in tools] == ["lazytools.datahub_list_datasets"]
        result = asyncio.run(asyncio.wait_for(tools[0].run(), timeout=30))
        assert '"pandas_preimported": true' in str(result).lower()
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
