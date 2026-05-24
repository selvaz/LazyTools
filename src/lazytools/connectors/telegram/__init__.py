"""Telegram connector: client and a guarded send tool.

Only building a real :class:`TelegramClient` from a bot token needs the
``telegram`` extra (``httpx``); the rest of the surface imports without it and
is fully testable with a fake client.
"""

from __future__ import annotations

from lazytools.connectors.telegram.client import TelegramClient, TelegramService
from lazytools.connectors.telegram.tools import TelegramSendBlocked, TelegramTools

__all__ = [
    "TelegramClient",
    "TelegramService",
    "TelegramTools",
    "TelegramSendBlocked",
]
