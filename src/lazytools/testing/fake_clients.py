"""In-memory fake service clients for testing guarded tools.

These satisfy the duck-typed ``GmailService`` / ``TelegramService`` /
``EdgarService`` / ``MarketDataAdapter`` Protocols without touching any
network, consolidating the per-test fakes that previously lived in each suite.
"""

from __future__ import annotations

import json
from typing import Any


class FakeGmailService:
    """In-memory :class:`~lazytools.connectors.gmail.client.GmailService`."""

    def __init__(self, messages: dict[str, dict[str, Any]] | None = None) -> None:
        self._messages = messages or {}
        self.drafts: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        # History / push surface (event-driven intake). Pre-seeded messages
        # are "before history starts": the cursor begins past them, so only
        # mail added via add_message() shows up in list_history_message_ids.
        self.calls: list[str] = []
        self.watches: list[dict[str, Any]] = []
        self.watch_stopped = False
        self.watch_expiration_ms = 9_999_999_999_999  # far future, override in tests
        self.history_expired = False  # set True to simulate a 404/expired cursor
        self._history_cursor = 1000
        self._history: list[tuple[int, str]] = []

    def list_message_ids(self, *, query: str | None = None, max_results: int = 25) -> list[str]:
        return list(self._messages)[:max_results]

    def get_message(self, message_id: str) -> dict[str, Any]:
        self.calls.append(f"get_message:{message_id}")
        return self._messages.get(message_id, {"id": message_id})

    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.drafts.append({"to": to, "subject": subject, "body": body})
        return {"id": f"draft-{len(self.drafts)}"}

    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"id": f"sent-{len(self.sent)}"}

    # -- history / push surface ----------------------------------------- #
    def add_message(self, message_id: str, raw: dict[str, Any] | None = None) -> None:
        """Simulate new mail arriving (advances the history cursor)."""
        self._messages[message_id] = raw or {"id": message_id}
        self._history_cursor += 1
        self._history.append((self._history_cursor, message_id))

    def get_history_id(self) -> str:
        self.calls.append("get_history_id")
        return str(self._history_cursor)

    def list_history_message_ids(self, *, start_history_id: str, max_results: int = 100) -> tuple[list[str], str]:
        self.calls.append("list_history")
        if self.history_expired:
            from lazytools.connectors.gmail.client import GmailHistoryExpired

            raise GmailHistoryExpired(f"history id {start_history_id!r} expired (fake)")
        start = int(start_history_id)
        pending = [(cursor, mid) for cursor, mid in self._history if cursor > start]
        batch = pending[:max_results]
        ids = [mid for _, mid in batch]
        # Same cursor-safety contract as GmailClient: when capped, the
        # cursor stops at the last *returned* entry so the next call
        # resumes there; only a fully drained walk returns "now".
        if len(pending) > len(batch):
            return ids, str(batch[-1][0])
        return ids, str(self._history_cursor)

    def watch(self, *, topic_name: str, label_ids: list[str] | None = None) -> dict[str, Any]:
        self.calls.append("watch")
        self.watches.append({"topic_name": topic_name, "label_ids": label_ids or ["INBOX"]})
        return {"historyId": str(self._history_cursor), "expiration": str(self.watch_expiration_ms)}

    def stop_watch(self) -> None:
        self.calls.append("stop_watch")
        self.watch_stopped = True


class FakeOutlookService:
    """In-memory :class:`~lazytools.connectors.outlook.client.OutlookService`.

    Like :class:`FakeGmailService`, but for the local-desktop Outlook surface:
    list/get reads from a pre-seeded ``messages`` dict (entry id → Gmail-shaped
    resource), and draft/send record their calls instead of touching COM.
    """

    def __init__(self, messages: dict[str, dict[str, Any]] | None = None) -> None:
        self._messages = messages or {}
        self.drafts: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.queries: list[str | None] = []

    def list_message_ids(self, *, query: str | None = None, max_results: int = 25) -> list[str]:
        self.queries.append(query)
        return list(self._messages)[:max_results]

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self._messages.get(message_id, {"id": message_id})

    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.drafts.append({"to": to, "subject": subject, "body": body})
        return {"id": f"draft-{len(self.drafts)}"}

    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"id": f"sent-{len(self.sent)}"}


class FakeTelegramService:
    """In-memory :class:`~lazytools.connectors.telegram.client.TelegramService`."""

    def __init__(self, updates: list[dict[str, Any]] | None = None) -> None:
        self._updates = updates or []
        self.sent: list[dict[str, Any]] = []
        self.offsets: list[int] = []

    def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        return [u for u in self._updates if u["update_id"] >= offset][:limit]

    def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": len(self.sent)}

    def send_document(
        self,
        *,
        chat_id: int | str,
        document: bytes,
        filename: str = "document",
        caption: str | None = None,
    ) -> dict[str, Any]:
        self.sent.append(
            {"chat_id": chat_id, "document": document, "filename": filename, "caption": caption}
        )
        return {"message_id": len(self.sent)}


class FakeEdgarClient:
    """In-memory :class:`~lazytools.connectors.edgar.client.EdgarService`.

    Ships small Apple-ish canned data (one company, one 10-K, minimal
    us-gaap companyfacts) so tool tests have something realistic to chew on;
    every dataset is a public attribute you can replace per test.
    """

    def __init__(self) -> None:
        self.companies: list[dict[str, str]] = [
            {"cik": "0000320193", "ticker": "AAPL", "title": "Apple Inc."},
        ]
        self.filings: list[dict[str, Any]] = [
            {
                "accession_no": "0000320193-24-000123",
                "form": "10-K",
                "filed_at": "2024-11-01",
                "report_date": "2024-09-28",
                "primary_document": "aapl-20240928.htm",
                "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
            },
        ]
        self.filing_text = "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nForm 10-K\nApple Inc."
        self.facts: dict[str, Any] = {
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {"USD": [{"end": "2024-09-28", "val": 391_035_000_000, "form": "10-K", "fy": 2024}]}
                    },
                    "NetIncomeLoss": {
                        "units": {"USD": [{"end": "2024-09-28", "val": 93_736_000_000, "form": "10-K", "fy": 2024}]}
                    },
                }
            },
        }
        self.calls: list[tuple[str, Any]] = []

    def resolve_company(self, query: str, *, limit: int = 10) -> list[dict[str, str]]:
        self.calls.append(("resolve_company", query))
        q = query.strip().lower()
        exact = [dict(c) for c in self.companies if c["ticker"].lower() == q]
        partial = [dict(c) for c in self.companies if c["ticker"].lower() != q and q in c["title"].lower()]
        return (exact + partial)[:limit]

    def list_filings(self, cik: str, *, form: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        self.calls.append(("list_filings", cik))
        matches = [dict(f) for f in self.filings if form is None or f["form"].upper() == form.upper()]
        return matches[:limit]

    def get_filing(self, cik: str, accession_no: str, *, primary_document: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_filing", accession_no))
        for filing in self.filings:
            if filing["accession_no"] == accession_no:
                return {
                    "accession_no": filing["accession_no"],
                    "form": filing["form"] if primary_document is None else None,
                    "url": filing["url"],
                    "content": self.filing_text,
                    "content_is_untrusted": True,
                }
        raise ValueError(f"accession {accession_no!r} not found in recent filings for CIK {cik}")

    def company_facts(self, cik: str) -> dict[str, Any]:
        self.calls.append(("company_facts", cik))
        return self.facts


class FakeMarketDataAdapter:
    """In-memory :class:`~lazytools.connectors.marketdata.adapters.MarketDataAdapter`.

    Serves a few Apple-ish daily rows (strings throughout, Decimal-safe);
    ``quote`` answers from the most recent row. Replace ``rows`` per test.
    """

    source = "stooq"

    def __init__(self, rows: list[dict[str, str]] | None = None) -> None:
        self.rows: list[dict[str, str]] = rows or [
            {
                "date": "2026-06-05",
                "open": "201.50",
                "high": "204.10",
                "low": "200.90",
                "close": "203.10",
                "volume": "48211000",
            },
            {
                "date": "2026-06-08",
                "open": "203.20",
                "high": "205.00",
                "low": "202.40",
                "close": "204.55",
                "volume": "45120000",
            },
            {
                "date": "2026-06-09",
                "open": "204.60",
                "high": "206.30",
                "low": "203.70",
                "close": "203.92",
                "volume": "50342000",
            },
        ]
        self.quote_calls: list[str] = []
        self.history_calls: list[tuple[str, str]] = []

    def quote(self, symbol: str) -> dict[str, str]:
        self.quote_calls.append(symbol)
        if not self.rows:
            raise ValueError(f"no data for {symbol!r}")
        last = self.rows[-1]
        return {"price": last["close"], "currency": "USD", "as_of": last["date"], "source": self.source}

    def history(self, symbol: str, *, range_: str = "1y") -> list[dict[str, str]]:
        self.history_calls.append((symbol, range_))
        return [dict(row) for row in self.rows]


class FakeDataHubBackend:
    """In-memory :class:`~lazytools.connectors.datahub.backend.DataHubBackend`.

    Returns canned JSON strings for every method (echoing the call name and
    arguments) so :class:`~lazytools.connectors.datahub.tools.DataHubTools` can
    be exercised with no real ``market_data_hub`` dependency. Every call is
    recorded in :attr:`calls` as ``(method, kwargs)`` for assertions.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        # Optional per-method override payloads (any JSON-serialisable object).
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _emit(self, method: str, **kwargs: Any) -> str:
        self.calls.append((method, kwargs))
        if method in self.responses:
            payload = self.responses[method]
        else:
            payload = {"tool": method, "args": kwargs, "fake": True}
        return json.dumps(payload, ensure_ascii=False, default=str)

    def list_datasets(self) -> str:
        return self._emit("list_datasets")

    def list_symbols(self, asset_class: str = "", area: str = "", sector: str = "", group: str = "") -> str:
        return self._emit("list_symbols", asset_class=asset_class, area=area, sector=sector, group=group)

    def list_sectors(self, area: str = "") -> str:
        return self._emit("list_sectors", area=area)

    def list_macro(self, frequency: str = "", category: str = "") -> str:
        return self._emit("list_macro", frequency=frequency, category=category)

    def list_indicators(self, pillar: str = "") -> str:
        return self._emit("list_indicators", pillar=pillar)

    def list_countries(self, region: str = "", income: str = "") -> str:
        return self._emit("list_countries", region=region, income=income)

    def describe(self, symbol_or_id: str) -> str:
        return self._emit("describe", symbol_or_id=symbol_or_id)

    def search(self, query: str) -> str:
        return self._emit("search", query=query)

    def get_series(
        self,
        symbols: str,
        start: str = "",
        end: str = "",
        domain: str = "prices",
        field: str = "adj_close",
        transform: str = "level",
        frequency: str = "",
    ) -> str:
        return self._emit(
            "get_series",
            symbols=symbols,
            start=start,
            end=end,
            domain=domain,
            field=field,
            transform=transform,
            frequency=frequency,
        )

    def get_returns(self, symbols: str, start: str = "", end: str = "", frequency: str = "W") -> str:
        return self._emit("get_returns", symbols=symbols, start=start, end=end, frequency=frequency)

    def get_coverage(self, symbols: str = "") -> str:
        return self._emit("get_coverage", symbols=symbols)
