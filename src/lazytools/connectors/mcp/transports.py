"""MCP transports — stdio (subprocess) and Streamable HTTP.

Each transport implements the small interface :class:`_Transport`. The
public :class:`MCP` factory in :mod:`lazytools.connectors.mcp.server` builds
transports lazily and only imports the official ``mcp`` SDK when a real
server is constructed — so importing :mod:`lazytools.connectors.mcp` itself is
cheap and never fails.

Loop / task affinity
--------------------
The official SDK's sessions are **loop- and task-affine**: the anyio task
groups inside ``stdio_client`` / ``streamablehttp_client`` must be entered and
exited in the *same task*, and the resulting streams only work on the loop
they were created on. Two mechanisms uphold that contract:

* Each SDK-backed transport runs its session inside a single long-lived
  **lifecycle task** (:meth:`_SdkTransport._run_session`): the task enters the
  SDK context, parks on an event, and unwinds the context itself when
  :meth:`close` sets the event. Enter and exit therefore always happen in the
  same task.
* :class:`~lazytools.connectors.mcp.server.MCPServer` dispatches every
  transport operation onto a dedicated background loop (:class:`_LoopRunner`),
  so the session is created, used, and closed on one loop regardless of which
  loop — or none — the caller happens to be on.
"""

from __future__ import annotations

import asyncio
import json
import threading
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from typing import Any


class _Transport(ABC):
    """Abstract MCP transport.  Sub-classes implement the JSON-RPC surface
    LazyBridge needs (initialise + list-tools + call-tool + close).

    **Loop contract.** :class:`~lazytools.connectors.mcp.server.MCPServer`
    runs *every* transport method — ``connect``, ``list_tools``,
    ``call_tool``, ``close`` — on the server's dedicated background loop,
    never on the caller's loop. The whole lifecycle therefore sees one
    consistent loop: create loop-affine resources (clients, sessions,
    queues) lazily inside :meth:`connect`, not in ``__init__`` on the
    caller's loop. (The caller's loop was never a safe home anyway — the
    sync ``as_tools()`` facade has no caller loop at all.)
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the server's tool catalogue.

        Each entry must be a dict with at least:

        - ``name``        — str
        - ``description`` — str
        - ``inputSchema`` — JSON Schema dict (object type with ``properties``)
        """

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...

    @abstractmethod
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Dedicated background loop — owns a transport's session.
# ---------------------------------------------------------------------------


class _LoopRunner:
    """A dedicated background event loop thread that owns an MCP session.

    The SDK's sessions and streams are loop-affine, so :class:`MCPServer`
    dispatches **all** transport operations here. Without this, the sync
    ``as_tools()`` facade would connect on a throwaway ``asyncio.run`` loop
    and every later tool call would fail with ``ClosedResourceError``.
    """

    def __init__(self, name: str) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._main, name=name, daemon=True)
        self._thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def run_sync(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coro`` on the owned loop, blocking the calling thread."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coro`` on the owned loop, awaitable from any *other* loop."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self._loop))

    def stop(self) -> None:
        """Stop the loop and join the thread. Idempotent."""
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()


# ---------------------------------------------------------------------------
# Shared SDK-session lifecycle.
# ---------------------------------------------------------------------------


class _SdkTransport(_Transport):
    """Common lifecycle for transports backed by the official ``mcp`` SDK.

    ``connect()`` spawns a single lifecycle task that enters the SDK client
    context, creates the ``ClientSession``, signals readiness, then parks on
    a close event. ``close()`` sets the event so the *same task* unwinds the
    context — anyio cancel scopes raise if entered and exited in different
    tasks (the failure mode of the previous ``AsyncExitStack`` approach).
    """

    def __init__(self) -> None:
        self._session: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._close_evt: asyncio.Event | None = None
        # Serialise concurrent connect() callers so two coroutines racing
        # past the early-return don't each spawn a lifecycle task and leak
        # the loser's subprocess/connection.
        self._connect_lock = asyncio.Lock()

    # -- subclass hooks -------------------------------------------------

    def _make_client_cm(self) -> Any:
        """Return the SDK's async context manager yielding the stream pair.

        Imports the SDK; raises a friendly ``ImportError`` when the ``mcp``
        extra is missing. Building the context manager object itself does not
        bind to a task — only ``__aenter__`` does, inside the lifecycle task.
        """
        raise NotImplementedError

    # -- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        if self._session is not None:
            return
        async with self._connect_lock:
            if self._session is not None:
                return
            cm = self._make_client_cm()  # may raise ImportError — no task spawned yet
            loop = asyncio.get_running_loop()
            started: asyncio.Future[None] = loop.create_future()
            close_evt = asyncio.Event()
            task = loop.create_task(self._run_session(cm, started, close_evt))
            try:
                await started
            except BaseException:
                close_evt.set()
                await asyncio.gather(task, return_exceptions=True)
                raise
            self._close_evt = close_evt
            self._task = task

    async def _run_session(
        self,
        cm: Any,
        started: asyncio.Future[None],
        close_evt: asyncio.Event,
    ) -> None:
        try:
            from mcp import ClientSession  # importable — _make_client_cm succeeded

            async with cm as streams:
                read, write = streams[0], streams[1]  # http yields a 3rd element
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    if not started.done():
                        started.set_result(None)
                    await close_evt.wait()
        except BaseException as exc:
            if not started.done():
                started.set_exception(exc if isinstance(exc, Exception) else RuntimeError(repr(exc)))
            else:
                raise
        finally:
            self._session = None

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._session is None:
            raise RuntimeError("list_tools called before connect()")
        result = await self._session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
            }
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("call_tool called before connect()")
        result = await self._session.call_tool(name, arguments=arguments)
        # MCP returns a list of content blocks; flatten text content into a string.
        return _extract_text(result)

    async def close(self) -> None:
        async with self._connect_lock:
            task, evt = self._task, self._close_evt
            self._task = None
            self._close_evt = None
            self._session = None
            if task is None:
                return
            if evt is not None:
                evt.set()
            # The lifecycle task unwinds the SDK context in its own task;
            # swallow any teardown noise (e.g. the subprocess already died).
            await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# stdio: subprocess via the official ``mcp`` SDK.
# ---------------------------------------------------------------------------


class StdioTransport(_SdkTransport):
    """Spawn an MCP server as a subprocess and speak JSON-RPC over stdio."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._command = command
        self._args = args or []
        self._env = env

    def _make_client_cm(self) -> Any:
        try:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:  # pragma: no cover — exercised only without [mcp]
            raise ImportError(
                "lazytools.connectors.mcp.MCP.stdio requires the official MCP SDK. Install with: pip install lazytoolkit[mcp]"
            ) from e
        params = StdioServerParameters(command=self._command, args=self._args, env=self._env)
        return stdio_client(params)


# ---------------------------------------------------------------------------
# Streamable HTTP — same SDK, different transport.
# ---------------------------------------------------------------------------


class HttpTransport(_SdkTransport):
    """Connect to an MCP server over Streamable HTTP."""

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        super().__init__()
        self._url = url
        self._headers = headers or {}

    def _make_client_cm(self) -> Any:
        try:
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "lazytools.connectors.mcp.MCP.http requires the official MCP SDK. Install with: pip install lazytoolkit[mcp]"
            ) from e
        return streamablehttp_client(self._url, headers=self._headers)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _extract_text(result: Any) -> str:
    """Pull text out of an MCP ``CallToolResult``; fall back to repr for non-text.

    A tool may also return ``structuredContent`` — the spec's authoritative
    structured-output field. Servers with an ``outputSchema`` typically mirror
    that data into a ``TextContent`` block for backwards compatibility, but some
    (e.g. Codex's MCP server) put load-bearing fields like ``threadId`` *only*
    in ``structuredContent``. We therefore append the JSON-serialised
    ``structuredContent`` after the text so the model can see it — unless its
    serialisation is already present in the text blocks (the mirrored case), to
    avoid duplicating output for the common server.
    """
    content = getattr(result, "content", None)
    parts: list[str] = []
    if content:
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(repr(block))
    text_out = "\n".join(parts)

    structured = getattr(result, "structuredContent", None)
    if structured:
        try:
            structured_json = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            structured_json = repr(structured)
        # Skip when the text already carries the same payload (mirrored servers).
        if structured_json not in text_out:
            text_out = f"{text_out}\n{structured_json}" if text_out else structured_json

    return text_out
