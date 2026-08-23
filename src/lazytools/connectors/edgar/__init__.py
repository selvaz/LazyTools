"""SEC EDGAR connector: transport client, plus a narrow, explicit-opt-in
research/citation tool provider.

Audit CA-03 removed the original ``EdgarTools``/``ResolveTools`` because
``ResolveTools.get_financial_facts`` let a finance agent fetch structured
XBRL facts directly, bypassing market-data-hub as the sole owner of that
data (its coverage tracking, ingestion ledger, provenance). For financial
FACTS, that finding still stands unchanged: agents reach them exclusively
through the hub-backed ``datahub_*`` tools, and ``company_facts`` is not
exposed here.

Filing TEXT is a different kind of thing -- unstructured narrative a company
wrote, not a time series market-data-hub tracks, closer to a crawled news
article than to a financial fact. :class:`~lazytools.connectors.edgar.tools.EdgarTools`
reintroduces read-only access to it (company/CIK lookup, filing listing,
filing text) for a caller that opts in explicitly -- it is not part of
``lazytools.connectors.fin.agents.pm_supervisor``'s default tool list, so no
bundled finance agent gains it silently.

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
from lazytools.connectors.edgar.tools import MAX_FILING_CHARS, EdgarTools

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MIN_REQUEST_INTERVAL",
    "EdgarClient",
    "EdgarService",
    "EdgarTools",
    "MAX_FILING_CHARS",
]
