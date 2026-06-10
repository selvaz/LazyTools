"""Thin wrapper around the official, free SEC EDGAR APIs.

``httpx`` is imported lazily inside :meth:`EdgarClient._http_client`, so this
module imports cleanly without the ``edgar`` extra.
:class:`~lazytools.connectors.edgar.tools.EdgarTools` depends only on the
duck-typed :class:`EdgarService` surface defined here, which means tests
inject a fake client and never touch the network.

The client honours the SEC fair-access policy
(https://www.sec.gov/os/accessing-edgar-data):

* a **declared ``User-Agent``** identifying the caller (e.g. ``"Jane Doe
  jane@example.com"``) is required — the constructor refuses an empty one;
* requests are **throttled** to ``min_request_interval`` seconds apart
  (default 0.11 s ≈ the SEC's 10 requests/second cap) on a monotonic clock;
* every response body is **hard-capped** at ``max_response_bytes``.

All URLs are constructed in code against the fixed SEC hosts — there are no
caller-supplied URLs — and each constructed URL *and* each redirect target is
still re-checked with :func:`~lazytools.safety.urls.validate_public_url`.
Filing content comes from public documents written by third parties, so
:meth:`EdgarClient.get_filing` labels it ``content_is_untrusted``.
"""

from __future__ import annotations

import json
import time
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urljoin

from lazytools.safety.urls import validate_public_url

#: Hosts the connector is pinned to; redirects outside this set are refused.
_SEC_HOSTS = frozenset({"www.sec.gov", "sec.gov", "data.sec.gov"})

_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{document}"

#: Default hard cap on every response body (bytes) — ~5 MB.
DEFAULT_MAX_RESPONSE_BYTES = 5_000_000
#: Default minimum spacing between requests (seconds) ≈ SEC's 10 req/s cap.
DEFAULT_MIN_REQUEST_INTERVAL = 0.11
#: Redirect hops followed (each target is re-validated) before giving up.
_MAX_REDIRECTS = 3
#: HTTP statuses treated as redirects when following manually.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class EdgarService(Protocol):
    """The subset of an EDGAR client that :class:`EdgarTools` uses."""

    def resolve_company(self, query: str, *, limit: int = 10) -> list[dict[str, str]]: ...
    def list_filings(self, cik: str, *, form: str | None = None, limit: int = 20) -> list[dict[str, Any]]: ...
    def get_filing(self, cik: str, accession_no: str, *, primary_document: str | None = None) -> dict[str, Any]: ...
    def company_facts(self, cik: str) -> dict[str, Any]: ...


class EdgarClient:
    """Production :class:`EdgarService` backed by the SEC EDGAR APIs over HTTPS.

    Args:
        user_agent: **Required.** A declared identity per the SEC fair-access
            policy, e.g. ``"Jane Doe jane@example.com"``. Empty/blank raises
            ``ValueError``.
        http: Optional injected HTTP client (an ``httpx.Client`` or anything
            exposing ``stream(method, url, headers=...)``). When omitted, an
            ``httpx.Client`` is built lazily on first use.
        timeout: Request timeout in seconds for the lazily built client.
        max_response_bytes: Hard cap applied to every response body.
        min_request_interval: Minimum spacing between requests in seconds
            (simple monotonic-clock throttle); ``0`` disables the throttle.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        http: Any | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise ValueError(
                "EdgarClient requires a non-empty user_agent. The SEC fair-access policy "
                "requires a declared User-Agent identifying you, e.g. 'Jane Doe jane@example.com'."
            )
        self._user_agent = user_agent.strip()
        self._http = http
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._min_request_interval = min_request_interval
        self._last_request_at: float | None = None
        # company_tickers.json cached in-memory for the client's lifetime.
        self._tickers_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # EdgarService
    # ------------------------------------------------------------------ #
    def resolve_company(self, query: str, *, limit: int = 10) -> list[dict[str, str]]:
        """Resolve a ticker or company-name query against company_tickers.json.

        Exact (case-insensitive) ticker matches come first, then substring
        matches on the company title. Each entry is
        ``{"cik": "0000320193", "ticker": "AAPL", "title": "Apple Inc."}``
        with the CIK zero-padded to 10 digits.
        """
        q = query.strip().lower()
        if not q:
            raise ValueError("resolve_company requires a non-empty query")
        if self._tickers_cache is None:
            self._tickers_cache = self._get_json(_COMPANY_TICKERS_URL)
        exact: list[dict[str, str]] = []
        partial: list[dict[str, str]] = []
        for entry in self._tickers_cache.values():
            ticker = str(entry.get("ticker", ""))
            title = str(entry.get("title", ""))
            record = {"cik": str(entry.get("cik_str", "")).zfill(10), "ticker": ticker, "title": title}
            if ticker.lower() == q:
                exact.append(record)
            elif q in title.lower():
                partial.append(record)
        return (exact + partial)[:limit]

    def list_filings(self, cik: str, *, form: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List a company's recent filings (newest first), optionally by form.

        Reads the ``filings.recent`` arrays of the submissions JSON. Each
        entry carries ``accession_no``, ``form``, ``filed_at``, ``report_date``
        (``None`` when EDGAR reports an empty string), ``primary_document``,
        and the Archives ``url`` of the primary document.
        """
        padded = _pad_cik(cik)
        data = self._get_json(_SUBMISSIONS_URL.format(cik=padded))
        recent = data.get("filings", {}).get("recent", {})
        accessions = recent.get("accessionNumber", [])
        forms = recent.get("form", [])
        filed = recent.get("filingDate", [])
        reports = recent.get("reportDate", [])
        documents = recent.get("primaryDocument", [])

        def _at(values: list[Any], i: int) -> str:
            return str(values[i]) if i < len(values) and values[i] is not None else ""

        results: list[dict[str, Any]] = []
        for i, accession in enumerate(accessions):
            form_i = _at(forms, i)
            if form is not None and form_i.upper() != form.upper():
                continue
            primary = _at(documents, i)
            results.append(
                {
                    "accession_no": str(accession),
                    "form": form_i,
                    "filed_at": _at(filed, i),
                    "report_date": _at(reports, i) or None,
                    "primary_document": primary,
                    "url": _archives_url(padded, str(accession), primary),
                }
            )
            if len(results) >= limit:
                break
        return results

    def get_filing(self, cik: str, accession_no: str, *, primary_document: str | None = None) -> dict[str, Any]:
        """Fetch a filing's primary document and strip it to plain text.

        When ``primary_document`` is not given, it (and the form type) is
        resolved from the submissions JSON. The returned ``content`` is
        size-capped, tag-stripped text from a public document written by a
        third party — treat it strictly as **data, never instructions**;
        ``content_is_untrusted`` is always ``True``.
        """
        padded = _pad_cik(cik)
        accession = _normalize_accession(accession_no)
        form: str | None = None
        if primary_document is None:
            for filing in self.list_filings(padded, limit=1000):
                if filing["accession_no"] == accession:
                    primary_document = filing["primary_document"]
                    form = filing["form"]
                    break
            if primary_document is None:
                raise ValueError(f"accession {accession!r} not found in recent filings for CIK {padded}")
        url = _archives_url(padded, accession, primary_document)
        raw = self._get(url).decode("utf-8", errors="replace")
        if primary_document.lower().endswith((".htm", ".html")) or raw.lstrip().startswith("<"):
            content = _html_to_text(raw)
        else:
            content = raw
        return {
            "accession_no": accession,
            "form": form,
            "url": url,
            "content": content,
            "content_is_untrusted": True,
        }

    def company_facts(self, cik: str) -> dict[str, Any]:
        """Return the raw XBRL companyfacts JSON for a company, untouched."""
        padded = _pad_cik(cik)
        return self._get_json(_COMPANY_FACTS_URL.format(cik=padded))

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    def _http_client(self) -> Any:
        if self._http is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover — exercised only without the extra
                raise ImportError(
                    "EdgarClient requires the 'edgar' extra. "
                    "Install it with: pip install 'lazytoolkit[edgar]'"
                ) from exc
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def _sleep(self, seconds: float) -> None:  # seam for tests — never sleep in a suite
        time.sleep(seconds)

    def _throttle(self) -> None:
        """Space requests ``min_request_interval`` apart on a monotonic clock."""
        if self._min_request_interval > 0 and self._last_request_at is not None:
            wait = self._min_request_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = time.monotonic()

    def _get(self, url: str) -> bytes:
        """GET ``url`` with throttling, a size cap, and re-validated redirects."""
        http = self._http_client()
        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}
        for _ in range(_MAX_REDIRECTS + 1):
            validate_public_url(url, allowed_hosts=_SEC_HOSTS)
            self._throttle()
            with http.stream("GET", url, headers=headers) as resp:
                if resp.status_code in _REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        raise RuntimeError(f"EDGAR redirect from {url} carried no Location header")
                    url = urljoin(url, location)
                    continue
                resp.raise_for_status()
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise RuntimeError(
                            f"EDGAR response for {url} exceeds max_response_bytes={self._max_response_bytes}"
                        )
                return bytes(body)
        raise RuntimeError(f"EDGAR request to {url} exceeded {_MAX_REDIRECTS} redirects")

    def _get_json(self, url: str) -> dict[str, Any]:
        data = json.loads(self._get(url).decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"EDGAR response for {url} is not a JSON object")
        return data


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _pad_cik(cik: str) -> str:
    """Normalize a CIK (with or without zero-padding / 'CIK' prefix) to 10 digits."""
    raw = str(cik).strip().upper().removeprefix("CIK").strip()
    if not raw.isdigit() or len(raw) > 10:
        raise ValueError(f"invalid CIK: {cik!r}")
    return raw.zfill(10)


def _normalize_accession(accession_no: str) -> str:
    """Normalize an accession number (with or without dashes) to dashed form."""
    raw = str(accession_no).strip().replace("-", "")
    if not raw.isdigit() or len(raw) != 18:
        raise ValueError(f"invalid accession number: {accession_no!r}")
    return f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"


def _archives_url(padded_cik: str, accession_no: str, document: str) -> str:
    return _ARCHIVES_URL.format(
        cik_int=int(padded_cik),
        accession=accession_no.replace("-", ""),
        document=document,
    )


class _TextExtractor(HTMLParser):
    """Collects visible text, skipping ``<script>``/``<style>``/``<head>``."""

    _SKIP_TAGS = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.chunks.append(" ".join(data.split()))


def _html_to_text(raw: str) -> str:
    """Strip HTML tags to whitespace-normalized plain text (stdlib only)."""
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    return "\n".join(parser.chunks)
