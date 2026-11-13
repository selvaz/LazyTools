"""Compatibility helpers for the official MCP Python SDK 1.x and 2.x.

SDK 2 replaced the transport-plus-``ClientSession``-plus-``initialize()``
client with :class:`mcp.Client`, renamed ``streamablehttp_client`` to
``streamable_http_client`` (headers now live on the HTTP client), and
renamed protocol attributes to snake_case (``input_schema``,
``structured_content``). SDK 1 keeps the previous names. The connector's
public factories stay the same either way: these helpers open a session
and read the fields under whichever spelling is installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any

_STDIO_HINT = (
    "lazytools.connectors.mcp.MCP.stdio requires the official MCP SDK. "
    'Install with: pip install "lazytoolkit[mcp] @ git+https://github.com/selvaz/LazyTools.git"'
)
_HTTP_HINT = (
    "lazytools.connectors.mcp.MCP.http requires the official MCP SDK. "
    'Install with: pip install "lazytoolkit[mcp] @ git+https://github.com/selvaz/LazyTools.git"'
)

# SDK 1's deprecated spelling. SDK 2 removed it in favour of streamable_http_client.
# Looked up by name so type-checking against SDK 2 does not require the attribute.
_V1_STREAMABLE_HTTP = "streamablehttp_client"

def mcp_major() -> int:
    """Major version of the installed ``mcp`` distribution.

    Call only after ``import mcp`` has succeeded. A copy that is not an
    installed distribution (tests sometimes inject one) is treated as 1,
    which keeps the legacy ``ClientSession`` path as the fallback.
    """
    try:
        return int(version("mcp").split(".", 1)[0])
    except (PackageNotFoundError, ValueError):
        return 1


def _require_mcp(hint: str) -> None:
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise ImportError(hint) from exc


def input_schema_of(tool: Any) -> dict[str, Any]:
    """Read a tool's JSON Schema under either SDK spelling.

    SDK 2 exposes ``input_schema``; SDK 1 exposes ``inputSchema``. An empty
    schema is filled with an object root, matching the historical connector
    behaviour for servers that omit one.
    """
    schema = getattr(tool, "input_schema", None)
    if not schema:
        schema = getattr(tool, "inputSchema", None)
    if not schema:
        return {"type": "object", "properties": {}}
    return schema


def structured_content_of(result: Any) -> Any:
    """Read structured tool output under either SDK spelling.

    SDK 2 uses ``structured_content``; SDK 1 uses ``structuredContent``.
    ``None`` means the field is absent. An empty mapping is returned as-is
    so the caller can decide whether it is worth surfacing.
    """
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    return structured


def open_stdio(
    command: str,
    args: list[str],
    env: dict[str, str] | None,
) -> Any:
    """Return an async context manager yielding a connected stdio session.

    SDK 2 yields :class:`mcp.Client` (handshake included). SDK 1 yields a
    ``ClientSession`` after ``initialize()``. Import errors raise before the
    context manager is entered, so a missing extra never starts a task.
    """
    _require_mcp(_STDIO_HINT)
    try:
        if mcp_major() >= 2:
            # Import before returning the context manager so a broken install
            # raises here, in ``connect``, before a lifecycle task is spawned.
            from mcp import Client as _Client
            from mcp import StdioServerParameters as _StdioServerParameters
            from mcp.client.stdio import stdio_client as _stdio_client

            del _Client, _StdioServerParameters, _stdio_client
            return _open_stdio_v2(command, args, env)
        from mcp import ClientSession as _ClientSession
        from mcp import StdioServerParameters as _StdioServerParameters
        from mcp.client.stdio import stdio_client as _stdio_client

        del _ClientSession, _StdioServerParameters, _stdio_client
        return _open_stdio_v1(command, args, env)
    except ImportError as exc:  # pragma: no cover — broken partial install
        raise ImportError(_STDIO_HINT) from exc


def open_http(url: str, headers: dict[str, str]) -> Any:
    """Return an async context manager yielding a connected HTTP session.

    Same split as :func:`open_stdio`. On SDK 2, custom headers are set on
    an ``httpx2`` client because ``streamable_http_client`` no longer takes
    ``headers=``; the timeouts match the SDK 1 ``streamablehttp_client``
    defaults (30s, 300s read) so a long-lived GET stream is not cut at
    httpx2's 5s default.
    """
    _require_mcp(_HTTP_HINT)
    try:
        if mcp_major() >= 2:
            from mcp import Client as _Client
            from mcp.client.streamable_http import streamable_http_client as _streamable_http_client

            del _Client, _streamable_http_client
            if headers:
                import httpx2 as _httpx2

                del _httpx2
            return _open_http_v2(url, headers)
        import mcp.client.streamable_http as streamable_http
        from mcp import ClientSession as _ClientSession

        # Fail here, before a lifecycle task is spawned, when the SDK 1 alias is gone.
        if not hasattr(streamable_http, _V1_STREAMABLE_HTTP):  # pragma: no cover
            raise ImportError(_V1_STREAMABLE_HTTP)
        del _ClientSession
        return _open_http_v1(url, headers)
    except ImportError as exc:  # pragma: no cover — broken partial install
        raise ImportError(_HTTP_HINT) from exc


@asynccontextmanager
async def _open_stdio_v1(
    command: str,
    args: list[str],
    env: dict[str, str] | None,
) -> AsyncIterator[Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command, args=args, env=env)
    async with stdio_client(params) as streams, ClientSession(streams[0], streams[1]) as session:
        await session.initialize()
        yield session


@asynccontextmanager
async def _open_stdio_v2(
    command: str,
    args: list[str],
    env: dict[str, str] | None,
) -> AsyncIterator[Any]:
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command, args=args, env=env)
    # Hand ``Client`` the transport, not the parameters object. Accepting a
    # bare ``StdioServerParameters`` arrived in SDK 2.1; 2.0.0 treats every
    # other target as a transport context manager and would try to enter the
    # model. ``stdio_client(...)`` is that transport on every 2.x release.
    # The default ``mode="auto"`` probes a 2.x server and falls back to the
    # initialize handshake for a 1.x server.
    async with Client(stdio_client(params)) as client:
        yield client


@asynccontextmanager
async def _open_http_v1(url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
    import mcp.client.streamable_http as streamable_http
    from mcp import ClientSession

    streamablehttp_client = getattr(streamable_http, _V1_STREAMABLE_HTTP)
    async with (
        streamablehttp_client(url, headers=headers or None) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        yield session


def _open_http_v2(url: str, headers: dict[str, str]) -> Any:
    # Import at construction so a missing SDK fails in ``connect`` before a
    # lifecycle task is spawned. The HTTP client itself is created inside
    # the context, on the task that enters it.
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    if not headers:

        @asynccontextmanager
        async def _open_plain() -> AsyncIterator[Any]:
            async with Client(url) as client:
                yield client

        return _open_plain()

    import httpx2

    timeout = httpx2.Timeout(30.0, read=300.0)

    @asynccontextmanager
    async def _open_with_headers() -> AsyncIterator[Any]:
        async with (
            # follow_redirects matches the SDK default client factory: a 307
            # trailing-slash redirect must not break a connection with headers.
            httpx2.AsyncClient(headers=dict(headers), timeout=timeout, follow_redirects=True) as http_client,
            Client(streamable_http_client(url, http_client=http_client)) as client,
        ):
            yield client

    return _open_with_headers()
