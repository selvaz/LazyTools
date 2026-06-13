"""Outlook connector: a local-desktop client + guarded draft/send tools.

The mirror of :mod:`lazytools.connectors.gmail`, but pointed at the copy of
Outlook already running and signed in on the user's Windows machine (over COM)
instead of the Gmail cloud API — so there are no OAuth credentials, no API
quota, and no Pub/Sub to set up. ``pywin32`` is only needed to build a real
:class:`OutlookClient` via :meth:`OutlookClient.connect`; the rest of the
surface imports without the ``outlook`` extra and is fully testable with a
fake client.

The ``Authentication-Results`` header lifted from each message is parsed with
the same provider-agnostic :func:`parse_authentication_results` used by the
Gmail connector.
"""

from __future__ import annotations

from lazytools.connectors.gmail.auth import parse_authentication_results
from lazytools.connectors.outlook.client import OutlookClient, OutlookService
from lazytools.connectors.outlook.tools import OutlookSendBlocked, OutlookTools

__all__ = [
    "OutlookClient",
    "OutlookService",
    "OutlookTools",
    "OutlookSendBlocked",
    "parse_authentication_results",
]
