"""Send each new approval ticket to the MCP client as a notification (SPEC R4.2).

One loop per client session reads the ``bridge_events`` tool after its own cursor
and sends ``notifications/message`` for every ``approval`` event. Delivery is best
effort: ``bridge_events`` stays the channel that does not lose events.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast
from weakref import WeakSet

import anyio
from lazybridge import Tool

logger = logging.getLogger("lazytools.mcp_server")

POLL_INTERVAL = 2.0


def register(server: Any, events_tool: Tool, *, sdk2: bool) -> None:
    """Run the loop for each session of ``server``, from ``notifications/initialized`` until it closes.

    Both SDK generations cancel a session's running handlers when its transport
    closes, which is what stops the loop.
    """
    import mcp.types

    types = cast(Any, mcp.types)  # the two SDK generations name these differently
    watched: WeakSet[Any] = WeakSet()

    async def push(session: Any, is_open: Callable[[], bool] = lambda: True) -> None:
        connection = getattr(session, "_connection", session)  # SDK 2 builds a new session object per message
        if connection in watched:  # a repeated ``initialized`` must not start a second loop
            return
        watched.add(connection)
        cursor = ""
        while is_open():
            try:
                result = await events_tool.run(cursor=cursor)
                events, cursor = result["events"], result["cursor"]
            except Exception:
                logger.warning("Approval notifications: reading bridge events failed", exc_info=True)
                events = []
            for event in events:
                if event["kind"] != "approval":
                    continue
                try:
                    await session.send_log_message(level="info", data=event)
                except Exception:
                    logger.debug("Approval notifications stopped: the client cannot be reached", exc_info=True)
                    return
            await anyio.sleep(POLL_INTERVAL)

    # A server that sends log notifications declares ``logging``, which the SDK
    # derives from this handler. Approval tickets are sent whatever level is set.
    if sdk2:

        async def initialized(ctx: Any, _params: Any) -> None:
            await push(ctx.session)

        async def set_level(_ctx: Any, _params: Any) -> Any:
            return types.EmptyResult()

        server.add_notification_handler("notifications/initialized", types.NotificationParams, initialized)
        server.add_request_handler("logging/setLevel", types.SetLevelRequestParams, set_level)
        return

    # SDK 1 passes no session to notification handlers; its message dispatcher has it.
    handle = server._handle_message

    async def handle_message(message: Any, session: Any, *args: Any, **kwargs: Any) -> None:
        await handle(message, session, *args, **kwargs)
        if isinstance(message, types.ClientNotification) and isinstance(message.root, types.InitializedNotification):
            # Early 1.x releases wait for handlers instead of cancelling them: end on a closed session too.
            incoming = session._incoming_message_stream_writer
            await push(session, lambda: incoming.statistics().open_send_streams > 0)

    async def set_level_v1(_level: Any) -> None:
        return None

    server._handle_message = handle_message
    server.set_logging_level()(set_level_v1)
