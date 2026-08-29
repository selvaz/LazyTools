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


def _fake_edgar_pad_cik(cik: str) -> str:
    """Same validation as the real EdgarClient's own ``_pad_cik``: a fake
    that never rejects a malformed CIK would let a caller's own bug (a
    ticker passed where a CIK was expected, a typo) pass silently in every
    test, then fail only against the real client in production."""
    raw = str(cik).strip().upper().removeprefix("CIK").strip()
    if not raw.isdigit() or len(raw) > 10:
        raise ValueError(f"invalid CIK: {cik!r}")
    return raw.zfill(10)


def _fake_edgar_normalize_accession(accession_no: str) -> str:
    """Same normalization as the real client: with or without dashes."""
    raw = str(accession_no).strip().replace("-", "")
    if not raw.isdigit() or len(raw) != 18:
        raise ValueError(f"invalid accession number: {accession_no!r}")
    return f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"


#: The one company self.filings/self.facts represent by default -- a
#: constant so it can be compared against, not just documentation.
_FAKE_EDGAR_DEFAULT_CIK = "0000320193"


class FakeEdgarClient:
    """In-memory :class:`~lazytools.connectors.edgar.client.EdgarService`.

    Ships small Apple-ish canned data (one company, a 10-K and an 8-K,
    minimal us-gaap companyfacts) so tool tests have something realistic to
    chew on; every dataset is a public attribute you can replace per test --
    kept in the SAME flat shapes (``filings: list[dict]``, ``facts: dict``)
    a first version of this class shipped with, so `fake.filings = [...]` /
    `fake.facts = {...}` keeps working unchanged; only ``default_cik``
    (which CIK that flat data represents) is new.

    Validates its CIK/accession arguments and normalizes accession numbers
    the same way the real client does, so code exercised against this fake
    fails here too, not only in production against the real EdgarClient --
    including a CIK that does not match ``default_cik``: a real Codex-review
    finding caught this fake serving Apple's data regardless of which CIK
    was asked for, exactly the kind of bug (a caller's own copy-paste from a
    different company) a fake exists to catch, not hide.
    """

    def __init__(self) -> None:
        self.default_cik = _FAKE_EDGAR_DEFAULT_CIK
        #: MMDD fiscal year end, matching the canned Apple data above.
        self.fye: str | None = "0928"
        self.companies: list[dict[str, str]] = [
            {"cik": _FAKE_EDGAR_DEFAULT_CIK, "ticker": "AAPL", "title": "Apple Inc."},
        ]
        self.filings: list[dict[str, Any]] = [
            {
                "accession_no": "0000320193-24-000123",
                "form": "10-K",
                "filed_at": "2024-11-01",
                "report_date": "2024-09-28",
                "items": [],
                "accepted_at": "2024-11-01T18:01:14.000Z",
                "primary_doc_description": "10-K",
                "primary_document": "aapl-20240928.htm",
                "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
            },
            {
                "accession_no": "0000320193-24-000100",
                "form": "8-K",
                "filed_at": "2024-08-02",
                "report_date": None,
                "items": ["2.02", "9.01"],
                "accepted_at": "2024-08-02T16:30:00.000Z",
                "primary_doc_description": "8-K",
                "primary_document": "aapl-8k.htm",
                "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000100/aapl-8k.htm",
            },
        ]
        # filing_text is the DEFAULT for any accession with no specific entry
        # below -- kept as a single public attribute so `fake.filing_text =
        # "custom"` (the original, and still simplest, per-test override)
        # keeps working unchanged. filing_texts differentiates specific
        # accessions from it -- added because the 8-K fixture above, without
        # one, silently returned the 10-K's own "Form 10-K" text.
        self.filing_text = "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nForm 10-K\nApple Inc."
        self.filing_texts: dict[str, str] = {
            "0000320193-24-000100": ("UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nForm 8-K\n"
                                     "Apple Inc. reports quarterly results."),
        }
        # The documents each filing contains. The 8-K carries its earnings
        # release as an exhibit, which is the shape the document tools exist
        # for: the primary document states the result, the exhibit has the
        # numbers.
        self.filing_documents: dict[str, list[dict[str, Any]]] = {
            "0000320193-24-000123": [
                {"sequence": "1", "type": "10-K", "description": "10-K",
                 "filename": "aapl-20240928.htm", "media_type": "text/html",
                 "url": "https://www.sec.gov/Archives/edgar/data/320193/"
                        "000032019324000123/aapl-20240928.htm"},
            ],
            "0000320193-24-000100": [
                {"sequence": "1", "type": "8-K", "description": "8-K",
                 "filename": "aapl-8k.htm", "media_type": "text/html",
                 "url": "https://www.sec.gov/Archives/edgar/data/320193/"
                        "000032019324000100/aapl-8k.htm"},
                {"sequence": "2", "type": "EX-99.1", "description": "EX-99.1",
                 "filename": "aapl-ex991.htm", "media_type": "text/html",
                 "url": "https://www.sec.gov/Archives/edgar/data/320193/"
                        "000032019324000100/aapl-ex991.htm"},
                # Deliberately unreadable, so a caller's handling of that is
                # exercised by the fake rather than only in production.
                {"sequence": "3", "type": "GRAPHIC", "description": None,
                 "filename": "logo.jpg", "media_type": "image/jpeg",
                 "url": "https://www.sec.gov/Archives/edgar/data/320193/"
                        "000032019324000100/logo.jpg"},
            ],
        }
        # Every readable document in the inventory has text. Review caught
        # only the exhibit having any, so fetching a primary document came
        # back empty with extraction_status "ok" -- a shape neither the real
        # client nor this fake's own get_filing() can produce, and one a
        # consumer could write a branch for.
        self.document_texts: dict[str, str] = {
            "aapl-20240928.htm": (
                "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nForm 10-K\nApple Inc."),
            "aapl-8k.htm": (
                "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nForm 8-K\n"
                "Apple Inc. reports quarterly results."),
            "aapl-ex991.htm": ("Apple Inc. reports fourth quarter results. "
                               "Revenue of $94.9 billion, up 6 percent."),
        }
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
        if not q:
            raise ValueError("resolve_company requires a non-empty query")
        exact = [dict(c) for c in self.companies if c["ticker"].lower() == q]
        partial = [dict(c) for c in self.companies if c["ticker"].lower() != q and q in c["title"].lower()]
        return (exact + partial)[:limit]

    def list_filings(
        self, cik: str, *, form: str | None = None, limit: int = 20, include_history: bool = False
    ) -> list[dict[str, Any]]:
        # include_history is accepted and ignored: this fake holds one flat
        # list with no notion of a recent-vs-archived split, and refusing the
        # argument would make code that passes it untestable against the fake.
        self.calls.append(("list_filings", cik))
        padded = _fake_edgar_pad_cik(cik)
        filings = self.filings if padded == _fake_edgar_pad_cik(self.default_cik) else []
        matches = [dict(f) for f in filings if form is None or f["form"].upper() == form.upper()]
        return matches[:limit]

    def list_filing_documents(self, cik: str, accession_no: str) -> list[dict[str, Any]]:
        """The documents one filing contains, from ``filing_documents``.

        Validates its arguments the same way the real client does, and an
        accession with no entry returns an empty inventory rather than
        another filing's documents -- a fake that answers for the wrong
        filing hides exactly the copy-paste this one exists to catch.
        """
        self.calls.append(("list_filing_documents", accession_no))
        padded = _fake_edgar_pad_cik(cik)
        normalizzato = _fake_edgar_normalize_accession(accession_no)
        documenti = (self.filing_documents.get(normalizzato, [])
                     if padded == _fake_edgar_pad_cik(self.default_cik) else [])
        if not documenti:
            # Mirrors the real client, which raises rather than describe a
            # filing as documentless -- no submission is. Returning [] here
            # would let a consumer write a branch for "a valid filing with no
            # documents", pass against this fake, and meet either documents
            # or an exception in production.
            raise RuntimeError(
                f"no documents parsed from the submission header for {normalizzato}; "
                f"FakeEdgarClient.filing_documents has no entry for it"
            )
        return [dict(d) for d in documenti]

    def get_filing_document(
        self, cik: str, accession_no: str, filename: str, *, raw: bool = False
    ) -> dict[str, Any]:
        """One named document, refused unless this filing contains it.

        The refusal is the real client's own: a caller names a document, not
        a URL. A fake that fetched anything asked of it would let a caller's
        path-building bug pass here and fail only in production.
        """
        self.calls.append(("get_filing_document", (accession_no, filename)))
        normalizzato = _fake_edgar_normalize_accession(accession_no)
        inventario = {d["filename"]: d for d in self.list_filing_documents(cik, accession_no)}
        voce = inventario.get(filename)
        if voce is None:
            raise ValueError(
                f"{filename!r} is not a document of filing {normalizzato}; "
                f"choose one of: {sorted(inventario)[:10]}"
            )
        leggibile = voce["media_type"] in {"text/html", "text/plain",
                                           "application/xml", "application/json"}
        contenuto = self.document_texts.get(filename, "") if leggibile else ""
        return {
            "accession_no": normalizzato,
            "filename": filename,
            "type": voce["type"],
            "description": voce["description"],
            "url": voce["url"],
            "media_type": voce["media_type"],
            "content": contenuto,
            "extraction_status": "ok" if leggibile else "unsupported",
            "size_bytes": len(contenuto.encode("utf-8")) or 1024,
            "content_is_untrusted": True,
        }

    def get_filing(self, cik: str, accession_no: str, *, primary_document: str | None = None) -> dict[str, Any]:
        """Mirrors the real client's own get_filing(): when primary_document
        is supplied directly, the accession is NEVER looked up against
        self.filings (matching EdgarClient, which just builds the Archives
        URL) -- a real Codex-review finding caught this fake requiring a
        list match even in that case, rejecting calls the real client would
        accept."""
        self.calls.append(("get_filing", accession_no))
        padded = _fake_edgar_pad_cik(cik)
        normalizzato = _fake_edgar_normalize_accession(accession_no)
        form = None
        documento = primary_document
        if documento is None:
            filings = self.filings if padded == _fake_edgar_pad_cik(self.default_cik) else []
            for filing in filings:
                if _fake_edgar_normalize_accession(filing["accession_no"]) == normalizzato:
                    documento = filing["primary_document"]
                    form = filing["form"]
                    break
            if documento is None:
                raise ValueError(f"accession {accession_no!r} not found in recent filings for CIK {cik}")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(padded)}/{normalizzato.replace('-', '')}/{documento}"
        return {
            "accession_no": normalizzato,
            "form": form,
            "url": url,
            "content": self.filing_texts.get(normalizzato, self.filing_text),
            "content_is_untrusted": True,
        }

    def company_facts(self, cik: str) -> dict[str, Any]:
        self.calls.append(("company_facts", cik))
        padded = _fake_edgar_pad_cik(cik)
        return self.facts if padded == _fake_edgar_pad_cik(self.default_cik) else {}

    def company_concept(self, cik: str, taxonomy: str, tag: str) -> dict[str, Any]:
        """One concept sliced out of ``facts``, in the real API's own shape.

        Slicing the existing companyfacts fixture rather than holding a second
        one keeps the two answers from drifting apart -- a fake whose concept
        view disagrees with its own facts view would pass tests that production
        fails.
        """
        self.calls.append(("company_concept", cik))
        padded = _fake_edgar_pad_cik(cik)
        if padded != _fake_edgar_pad_cik(self.default_cik):
            return {}
        body = (self.facts.get("facts", {}).get(taxonomy) or {}).get(tag)
        if body is None:
            # Same shape as the wrong-CIK answer above: both are "this fake has
            # nothing for you", and raising for one while returning {} for the
            # other would let a caller be tested against two contradictory
            # absence behaviours depending only on which key was wrong.
            return {}
        return {
            "cik": int(padded),
            "taxonomy": taxonomy,
            "tag": tag,
            "label": body.get("label"),
            "units": body.get("units", {}),
        }

    def fiscal_year_end(self, cik: str) -> str | None:
        """``MMDD`` year end; Apple's is late September, hence ``"0928"``."""
        self.calls.append(("fiscal_year_end", cik))
        padded = _fake_edgar_pad_cik(cik)
        return self.fye if padded == _fake_edgar_pad_cik(self.default_cik) else None

    def issuer_profile(self, cik: str) -> dict[str, Any]:
        """Identity + calendar for one CIK; empty name/tickers for a stranger."""
        self.calls.append(("issuer_profile", cik))
        padded = _fake_edgar_pad_cik(cik)
        if padded != _fake_edgar_pad_cik(self.default_cik):
            return {"cik": padded, "name": "", "tickers": [], "fiscal_year_end": None}
        return {
            "cik": padded,
            "name": self.companies[0]["title"],
            "tickers": [self.companies[0]["ticker"]],
            "fiscal_year_end": self.fye,
        }


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

    def resolve_instrument(self, query: str, exchange: str = "", currency: str = "") -> str:
        return self._emit("resolve_instrument", query=query, exchange=exchange, currency=currency)

    def get_price_summary(self, query: str, start: str = "", end: str = "") -> str:
        return self._emit("get_price_summary", query=query, start=start, end=end)

    def get_financials_coverage(self, query: str = "") -> str:
        return self._emit("get_financials_coverage", query=query)

    def get_financial_facts(self, query: str, line: str = "", forms: str = "", limit: int = 25) -> str:
        return self._emit("get_financial_facts", query=query, line=line, forms=forms, limit=limit)

    def get_statement(self, query: str, statement: str = "", periods: int = 8) -> str:
        return self._emit("get_statement", query=query, statement=statement, periods=periods)

    def get_job_status(self, job_id: str) -> str:
        return self._emit("get_job_status", job_id=job_id)

    def get_ingestion_health(self) -> str:
        return self._emit("get_ingestion_health")

    def calendar_vocabulary(self) -> str:
        return self._emit("calendar_vocabulary")

    def calendar_series(self, day: str = "", from_day: str = "", to_day: str = "",
                        country: str = "", area: str = "", category: str = "",
                        tags: str = "", data_type: str = "", criticality: str = "",
                        released_only: bool = False) -> str:
        return self._emit("calendar_series", day=day, from_day=from_day,
                          to_day=to_day, country=country, area=area,
                          category=category, tags=tags, data_type=data_type,
                          criticality=criticality, released_only=released_only)

    def register_listing(self, symbol: str, exchange: str, currency: str, kind: str = "EQUITY", name: str = "", provider: str = "yahoo", provider_symbol: str = "") -> str:
        return self._emit("register_listing", symbol=symbol, exchange=exchange,
                          currency=currency, kind=kind, name=name,
                          provider=provider, provider_symbol=provider_symbol)

    def ensure_price_history(self, query: str, start: str = "", end: str = "") -> str:
        return self._emit("ensure_price_history", query=query, start=start, end=end)

    def ensure_financials(self, query: str) -> str:
        return self._emit("ensure_financials", query=query)
