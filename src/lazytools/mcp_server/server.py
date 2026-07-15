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
    "_export_",
    "_fit",
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
