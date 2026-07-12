"""SEC EDGAR connector: transport CLIENT only.

The LLM-facing ``EdgarTools`` provider was REMOVED (audit CA-03, no
compatibility window needed): agents reach SEC data exclusively through the
hub-backed ``datahub_*`` financial tools. The client stays as injectable
plumbing for non-agent code (e.g. LazyFin's EdgarClientLike protocol).

Only building a real :class:`EdgarClient` needs the ``edgar`` extra
(``httpx``); the rest of the surface imports without it and is fully testable
with a fake client. The SEC fair-access policy requires a declared
``User-Agent`` (e.g. ``"Jane Doe jane@example.com"``) — the client refuses to
start without one — and the client throttles to ~10 requests/second and caps
every response body.
"""

from __future__ import annotations

from lazytools.connectors.edgar.client import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MIN_REQUEST_INTERVAL,
    EdgarClient,
    EdgarService,
)

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MIN_REQUEST_INTERVAL",
    "EdgarClient",
    "EdgarService",
]
