"""Gmail connector: client, guarded draft/send tools, and auth-header parsing.

The Google client libraries are only needed to build a real :class:`GmailClient`
from credentials (``GmailClient.from_credentials``); the rest of the surface
imports without the ``gmail`` extra and is fully testable with a fake client.
"""

from __future__ import annotations

from lazytools.connectors.gmail.auth import parse_authentication_results
from lazytools.connectors.gmail.client import GmailClient, GmailHistoryExpired, GmailService
from lazytools.connectors.gmail.tools import GmailSendBlocked, GmailTools

__all__ = [
    "GmailClient",
    "GmailHistoryExpired",
    "GmailService",
    "GmailTools",
    "GmailSendBlocked",
    "parse_authentication_results",
]
