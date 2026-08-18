"""Core of the LazyTools MCP *server*.

This is the mirror of :mod:`lazytools.connectors.mcp` (the MCP *client*):
instead of consuming an external MCP server, it **exposes** LazyTools'
own tool providers over the Model Context Protocol so that any MCP host
(Claude Desktop, Claude Code, Codex, …) can call ``datahub_*``,
``statistical_*``, ``regime_*`` and friends as native tools.

The bridge is thin because LazyBridge already normalises everything behind
a single :class:`lazybridge.Tool` abstraction:

* ``tool.definition()`` yields a JSON-Schema ``ToolDefinition`` — exactly
  what MCP's ``Tool.inputSchema`` wants;
* ``await tool.run(**kwargs)`` dispatches with argument validation built in.

So the whole server is: expand providers → list them → dispatch calls.

The ``mcp`` SDK is imported lazily (mirroring the client connector) so this
module imports fine without the ``[mcp]`` extra; only :func:`build_server`
and :func:`serve_stdio` require it.

Safety
------
The server is **read-only by default**. ``read_only=True`` (the default)
drops any tool whose name matches :data:`UNSAFE_TOOL_PATTERNS` as a
belt-and-suspenders guard on top of the provider-level gating (e.g.
``DataHubTools()`` already omits the refresh writers, ``RegimeTools()``
already omits the fit/persist/delete writers). Pass ``read_only=False``
only when you have deliberately wired confirmation/allow-list gating
around the mutating tools yourself.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from lazybridge import Tool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.lowlevel import Server

logger = logging.getLogger("lazytools.mcp_server")

#: Substrings that mark a tool as mutating / side-effecting. When
#: ``read_only=True`` (the default) any tool whose name contains one of
#: these is dropped from the served surface with a logged warning.
#:
#: This is a **secondary** guard, not the authoritative one: a tool's name
#: cannot fully classify its effect (e.g. ``regime_load_from_datahub`` and
#: ``regime_generate_plots`` mutate the depot yet match nothing here). The
#: real gate is provider-level configuration — ``default_providers()``
#: constructs every provider read-only (``RegimeTools()`` with
#: ``allow_write=False`` never even *emits* its writers; ``DataHubTools()``
#: omits the refresh writers). The patterns below are the coarse net that
#: still trips on the obvious writers if a write-enabled provider is passed
#: to ``build_server(read_only=True)`` by mistake. Kept conservative so it
#: never drops a real reader (the shipped ``datahub_get_*`` /
#: ``statistical_*`` / ``regime_*`` readers match none of these), and
#: overridable per call via ``unsafe_patterns=``.
UNSAFE_TOOL_PATTERNS: tuple[str, ...] = (
    "_send",
    "_write",
    "_delete",
    "_register",
    "_ensure_",
    "_refresh",
    "_persist",
    "_save",
    "save_",
    "_export_",
    "_fit",
    "_init_db",
    "optimizer_run",
    "optimizer_backtest",
    "optimizer_create",
    "tree_estimate",
    "tree_backtest",
    "_create_draft",
    "-specialist",
    # Not writers (both coding runtimes are configured read-only), but each
    # call spends a real model turn — same reason "-specialist" is here: an
    # LLM-driven tool has no place on the default, deterministic surface.
    # Covers codex_code_review / codex_ask / codex_review_changes and the
    # Claude Code twins.
    "codex_",
    "claude_code_review",
    "claude_ask",
)


def _is_unsafe(name: str, patterns: Iterable[str]) -> bool:
    lowered = name.lower()
    return any(p in lowered for p in patterns)


def expand_tools(
    providers: Sequence[Any],
    *,
    read_only: bool = True,
    unsafe_patterns: Iterable[str] = UNSAFE_TOOL_PATTERNS,
) -> dict[str, Tool]:
    """Expand ``providers`` into a ``name -> Tool`` map, isolating failures.

    Each item may be a :class:`lazybridge.Tool`, a plain callable, an Agent,
    or a ``ToolProvider`` (anything with ``as_tools()``). Providers are
    expanded **independently**: if one raises on expansion — typically an
    ``ImportError`` because its optional extra is not installed (e.g.
    ``RegimeTools`` without ``lazystats[regimes]``) — it is skipped with a
    warning and the rest still load. This is what lets a bare
    ``pip install lazytoolkit[mcp]`` serve datahub + statistical while
    regimes/web light up only once their extras are present.

    On a name collision the last registration wins (with a warning),
    mirroring ``build_tool_map(collision_policy="replace")``.
    """
    patterns = tuple(unsafe_patterns)
    result: dict[str, Tool] = {}
    for provider in providers:
        label = getattr(provider, "name", None) or type(provider).__name__
        try:
            if getattr(provider, "_is_lazy_tool_provider", False):
                tools = list(provider.as_tools())
            elif isinstance(provider, Tool):
                tools = [provider]
            elif getattr(provider, "_is_lazy_agent", False):
                tools = [Tool.wrap(provider)]  # agents carry their own name
            elif callable(provider):
                # Plain function: the Tool constructor defaults the name to
                # ``func.__name__``. (``Tool.wrap`` *requires* an explicit
                # name for callables and would raise here, silently dropping
                # the function via the except below.)
                tools = [Tool(provider)]
            else:
                raise TypeError(f"cannot expose {type(provider).__name__!r} as a tool")
        except Exception as exc:
            logger.warning("Skipping tool provider %s: %s", label, exc)
            continue

        for tool in tools:
            if read_only and _is_unsafe(tool.name, patterns):
                logger.warning(
                    "read_only: dropping mutating tool %r (matched unsafe pattern). "
                    "Pass read_only=False to expose it.",
                    tool.name,
                )
                continue
            if tool.name in result:
                logger.warning("Tool name collision on %r — keeping the later registration.", tool.name)
            result[tool.name] = tool
    return result


def _json_default(obj: Any) -> Any:
    """Best-effort JSON coercion for connector return values.

    Handles pydantic models, dataclasses, and the ``AnalysisResult`` /
    envelope-ish objects the analysis tools return, falling back to
    ``str`` so serialization can never raise.
    """
    for meth in ("model_dump", "to_dict", "dict", "_asdict"):
        fn = getattr(obj, meth, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return str(obj)


def result_to_text(result: Any) -> str:
    """Serialize a tool's return value to a single text payload for MCP.

    Strings pass through untouched; bytes are decoded as UTF-8; everything
    else is rendered as pretty JSON (pydantic ``model_dump_json`` /
    ``to_json`` fast-paths first), never raising.
    """
    if result is None:
        return "null"
    if isinstance(result, str):
        return result
    if isinstance(result, (bytes, bytearray)):
        try:
            return bytes(result).decode("utf-8")
        except Exception:
            return repr(bytes(result))

    dump_json = getattr(result, "model_dump_json", None)
    if callable(dump_json):
        try:
            return dump_json()
        except Exception:
            pass

    to_json = getattr(result, "to_json", None)
    if callable(to_json):
        try:
            value = to_json()
            return value if isinstance(value, str) else json.dumps(value, default=_json_default, ensure_ascii=False)
        except Exception:
            pass

    try:
        return json.dumps(result, default=_json_default, ensure_ascii=False, indent=2)
    except Exception:
        return str(result)


def build_server(
    providers: Sequence[Any],
    *,
    name: str = "lazytools",
    version: str | None = None,
    read_only: bool = True,
    unsafe_patterns: Iterable[str] = UNSAFE_TOOL_PATTERNS,
    instructions: str | None = None,
) -> Server:
    """Build a low-level MCP :class:`~mcp.server.lowlevel.Server` for ``providers``.

    The returned server has two handlers wired:

    * ``list_tools`` → one MCP ``Tool`` per expanded LazyBridge tool, with
      ``inputSchema`` taken verbatim from ``tool.definition().parameters``;
    * ``call_tool`` → ``await tool.run(**arguments)``, the return value
      serialized via :func:`result_to_text`. Tool errors are returned as
      MCP error results (``isError=True``) instead of crashing the session.

    Requires the ``mcp`` extra (``pip install lazytoolkit[mcp]``).
    """
    try:
        import mcp.types as types
        from mcp.server.lowlevel import Server
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise ImportError(
            "lazytools.mcp_server requires the MCP SDK: pip install 'lazytoolkit[mcp]'"
        ) from exc

    tool_map = expand_tools(providers, read_only=read_only, unsafe_patterns=unsafe_patterns)
    logger.info("LazyTools MCP server exposing %d tool(s): %s", len(tool_map), ", ".join(sorted(tool_map)))

    server: Server = Server(name, version=version, instructions=instructions)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        listed: list[types.Tool] = []
        for tool in tool_map.values():
            definition = tool.definition()
            listed.append(
                types.Tool(
                    name=definition.name,
                    description=definition.description or "",
                    inputSchema=definition.parameters,
                )
            )
        return listed

    @server.call_tool()
    async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        tool = tool_map.get(tool_name)
        if tool is None:
            return [types.TextContent(type="text", text=f"Unknown tool: {tool_name!r}")]
        result = await tool.run(**(arguments or {}))
        return [types.TextContent(type="text", text=result_to_text(result))]

    return server


async def serve_stdio(server: Server) -> None:
    """Run ``server`` over stdio until the client disconnects.

    This is the transport Claude Desktop / Claude Code launch: the host
    spawns the process and speaks JSON-RPC over its stdin/stdout.
    """
    from mcp.server.stdio import stdio_server

    init_options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def http_app(
    server: Server,
    *,
    path: str = "/mcp",
    json_response: bool = False,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
):
    """A Starlette app serving ``server`` over MCP's Streamable HTTP transport.

    Why this exists alongside ``serve_stdio``: a stdio server's lifetime is
    the client's. The host spawns it, so the tool list is fixed at spawn and
    picking up a changed tool means restarting the *client*. Worse, a stdio
    server that exits is not restarted -- Claude Code reconnects HTTP and SSE
    servers automatically (five attempts, exponential backoff from one
    second) but states plainly that "stdio servers are local processes and
    are not reconnected automatically".

    Over HTTP the process is independent of the client, and a reconnect
    performs a fresh ``initialize`` + ``tools/list``. So a tool change is
    picked up by restarting this process -- seconds -- rather than the
    editor, and no in-process module reloading is involved, which is the part
    that would have been fragile: re-importing a package to pick up new tool
    definitions leaves stale references behind, while a new process cannot.

    Returned rather than run, so a caller can mount it, wrap it, or test it
    without a live port.

    ``allowed_hosts``/``allowed_origins`` drive DNS-rebinding protection, and
    are not optional hardening. The SDK turns that protection OFF when a
    transport is constructed without security settings -- ``"If not
    specified, disable DNS rebinding protection by default for backwards
    compatibility"`` -- and a bare loopback bind is not a defence: any page
    open in a browser on this machine can POST to 127.0.0.1, and with
    ``--allow-unsafe`` these tools write production databases and send
    messages. Origin is validated when present and absent Origin is allowed,
    which is what lets a native client through while a cross-origin browser
    request is refused.
    """
    import contextlib

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.routing import Mount

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts or []),
        allowed_origins=list(allowed_origins or []),
    )
    manager = StreamableHTTPSessionManager(
        app=server, json_response=json_response, security_settings=security
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # The manager's own task group. It is single-use by contract -- the
        # instance cannot be reused once this exits -- which is another
        # reason a code change restarts the process rather than rebuilding
        # the server in place.
        async with manager.run():
            yield

    # Both spellings mount the transport directly. Left to Starlette's own
    # slash handling, a request to "/mcp" is answered with a 307 to "/mcp/"
    # BEFORE the mounted app runs, so the Host check never sees it and the
    # redirect's Location echoes back whatever Host the caller sent. That is
    # not a bypass -- a rebound name still meets 421 on the redirected
    # request -- but it spends a round trip and reflects an attacker's host,
    # neither of which a server that only ever answers one path needs to do.
    return Starlette(
        routes=[
            Mount(path, app=manager.handle_request),
            Mount(path + "/", app=manager.handle_request),
        ],
        lifespan=lifespan,
    )


def serve_http(server: Server, *, host: str = "127.0.0.1", port: int = 8787,
               path: str = "/mcp", json_response: bool = False,
               allowed_hosts: Sequence[str] | None = None,
               allowed_origins: Sequence[str] | None = None) -> None:
    """Serve ``server`` over Streamable HTTP until interrupted.

    Binds to loopback by default: these tools read (and with
    ``--allow-unsafe`` write) local production databases, so the default must
    not be reachable from the network.

    ``allowed_hosts`` defaults to the address actually bound, plus the
    ``localhost`` spelling of it, since a client may use either. Anything
    else is refused with 421 -- which is the point: a Host header this server
    did not expect is how a rebound DNS name reaches a loopback socket.
    """
    import uvicorn

    if allowed_hosts is None:
        allowed_hosts = [f"{host}:{port}", f"localhost:{port}"]
        if host == "127.0.0.1":
            allowed_hosts.append(f"[::1]:{port}")

    uvicorn.run(
        http_app(
            server,
            path=path,
            json_response=json_response,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
        host=host,
        port=port,
        log_level="info",
    )
