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
import re
import time
from html import unescape as _html_unescape
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
#: The submission's SGML header. Chosen over ``index.json`` deliberately:
#: measured against a real Apple earnings 8-K, index.json's ``type`` field is
#: the directory ICON name ("text.gif", "image2.gif") and carries no exhibit
#: type or description at all, so an exhibit cannot be identified from it.
#: This file has the real ``<TYPE>``/``<SEQUENCE>``/``<FILENAME>``/
#: ``<DESCRIPTION>`` per document -- HTML-escaped, hence the unescape below.
_INDEX_HEADERS_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{dashed}-index-headers.html"
)

#: Default hard cap on every response body (bytes) — ~5 MB.
DEFAULT_MAX_RESPONSE_BYTES = 5_000_000
#: Default minimum spacing between requests (seconds) ≈ SEC's 10 req/s cap.
DEFAULT_MIN_REQUEST_INTERVAL = 0.11
#: Redirect hops followed (each target is re-validated) before giving up.
_MAX_REDIRECTS = 3
#: HTTP statuses treated as redirects when following manually.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Extension -> media type, for the documents a filing actually contains.
_MEDIA_TYPES = {
    ".htm": "text/html", ".html": "text/html", ".txt": "text/plain",
    ".xml": "application/xml", ".xsd": "application/xml",
    ".json": "application/json", ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".zip": "application/zip",
}
#: Media types text IS extracted from. An allowlist, not a list of binaries
#: to skip: review caught the inverse letting an unlisted format through --
#: a .xlsx resolves to application/octet-stream, which was in no denylist, so
#: it was decoded as UTF-8 and returned as successfully extracted text made
#: of replacement characters. Anything not named here is reported unsupported,
#: which is true of a format we have never seen as well as of a PDF.
_TEXT_MEDIA = frozenset({
    "text/html", "text/plain", "application/xml", "application/json",
})


class EdgarService(Protocol):
    """The subset of an EDGAR client that :class:`EdgarTools` uses."""

    def resolve_company(self, query: str, *, limit: int = 10) -> list[dict[str, str]]: ...
    def list_filings(self, cik: str, *, form: str | None = None, limit: int = 20) -> list[dict[str, Any]]: ...
    def get_filing(self, cik: str, accession_no: str, *, primary_document: str | None = None) -> dict[str, Any]: ...
    def list_filing_documents(self, cik: str, accession_no: str) -> list[dict[str, Any]]: ...
    def get_filing_document(self, cik: str, accession_no: str, filename: str) -> dict[str, Any]: ...
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
        (``None`` when EDGAR reports an empty string -- and note this is the
        covered PERIOD, e.g. a quarter-end, not the submission date; it is
        not a substitute for ``filed_at`` when checking filing recency),
        ``items`` (an 8-K's own item codes, e.g. ``["2.02", "9.01"]`` for a
        results-of-operations 8-K; empty for every other form), ``primary_document``,
        and the Archives ``url`` of the primary document.
        """
        padded = _pad_cik(cik)
        data = self._get_json(_SUBMISSIONS_URL.format(cik=padded))
        recent = data.get("filings", {}).get("recent", {})
        accessions = recent.get("accessionNumber", [])
        forms = recent.get("form", [])
        filed = recent.get("filingDate", [])
        reports = recent.get("reportDate", [])
        items = recent.get("items", [])
        documents = recent.get("primaryDocument", [])
        accepted = recent.get("acceptanceDateTime", [])
        descriptions = recent.get("primaryDocDescription", [])

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
                    "items": [c.strip() for c in _at(items, i).split(",") if c.strip()],
                    # The instant EDGAR accepted the submission, not just the
                    # day: "was this filing available at 22:30?" cannot be
                    # answered from a date, and a caller reporting on one
                    # evening needs to answer exactly that.
                    "accepted_at": _at(accepted, i) or None,
                    "primary_doc_description": _at(descriptions, i) or None,
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

    def list_filing_documents(self, cik: str, accession_no: str) -> list[dict[str, Any]]:
        """Every document in one submission, with its exhibit type.

        The primary document is only ever part of a filing. An earnings 8-K
        typically states its result in Item 2.02 and carries the release
        itself as an exhibit, so a caller that can reach only the primary
        document can reach the announcement but not the numbers.

        Read from the submission's SGML header rather than ``index.json``:
        measured against a real Apple earnings 8-K, index.json's ``type`` is
        the directory icon ("text.gif") and there is no description field at
        all, so nothing there identifies an exhibit.

        Each entry carries ``sequence``, ``type`` (e.g. ``"EX-99.1"``),
        ``description`` (often as uninformative as the type -- Apple's
        earnings release describes itself as "EX-99.1"), ``filename``,
        ``url``, and ``media_type`` guessed from the extension.
        """
        padded = _pad_cik(cik)
        dashed = _normalize_accession(accession_no)
        url = _INDEX_HEADERS_URL.format(
            cik_int=int(padded), accession=dashed.replace("-", ""), dashed=dashed)
        # Unescaped first: the header is served inside an HTML page, so its
        # SGML tags arrive as &lt;TYPE&gt; and a naive parse finds nothing.
        raw = _html_unescape(self._get(url).decode("utf-8", errors="replace"))
        documents: list[dict[str, Any]] = []
        for block in re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", raw, re.S | re.I):
            filename = _sgml_field(block, "FILENAME")
            if not filename:
                continue
            documents.append({
                "sequence": _sgml_field(block, "SEQUENCE"),
                "type": _sgml_field(block, "TYPE"),
                "description": _sgml_field(block, "DESCRIPTION"),
                "filename": filename,
                "media_type": _media_type(filename),
                "url": _archives_url(padded, dashed, filename),
            })
        if not documents:
            # A real submission always contains at least its own primary
            # document, so an empty inventory is this parse failing, not the
            # filing being empty -- and returning [] would tell the caller
            # the second. Review found one shape where the header 404s
            # instead (a 1994 accession), which raises on its own; this
            # covers a 200 whose body we could not read.
            raise RuntimeError(
                f"no documents parsed from the submission header for {dashed}; "
                f"the filing index at {_archives_url(padded, dashed, '')} lists them"
            )
        return documents

    def get_filing_document(self, cik: str, accession_no: str, filename: str) -> dict[str, Any]:
        """Fetch one named document from a submission, as text.

        ``filename`` must be one this submission actually contains: it is
        checked against :meth:`list_filing_documents` rather than pasted into
        an Archives URL. A caller-supplied path would otherwise decide what
        this client fetches, which is not a decision a caller gets to make
        even against a host we pin.

        Binary documents are not decoded into text -- an image or a PDF comes
        back with ``extraction_status`` saying so and empty ``content``,
        rather than a page of replacement characters pretending to be prose.
        """
        padded = _pad_cik(cik)
        dashed = _normalize_accession(accession_no)
        inventory = {d["filename"]: d for d in self.list_filing_documents(padded, dashed)}
        entry = inventory.get(filename)
        if entry is None:
            raise ValueError(
                f"{filename!r} is not a document of filing {dashed}; "
                f"choose one of: {sorted(inventory)[:10]}"
            )
        media = entry["media_type"]
        if media not in _TEXT_MEDIA:
            # Not fetched at all. The inventory already says this is a JPEG or
            # a PDF, so downloading it to discard it spends a request against
            # the SEC's rate limit and the caller's deadline to learn what we
            # already knew. size_bytes is the inventory's, or None.
            return {
                "accession_no": dashed, "filename": filename,
                "type": entry["type"], "description": entry["description"],
                "url": entry["url"], "media_type": media,
                "content": "",
                "extraction_status": "unsupported",
                "size_bytes": entry.get("size_bytes"),
                "content_is_untrusted": True,
            }
        body = self._get(entry["url"])
        raw = body.decode("utf-8", errors="replace")
        content = _html_to_text(raw) if _looks_like_html(filename, raw) else raw
        return {
            "accession_no": dashed, "filename": filename,
            "type": entry["type"], "description": entry["description"],
            "url": entry["url"], "media_type": media,
            "content": content,
            "extraction_status": "ok",
            "size_bytes": len(body),
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
                    'Install it with: pip install "lazytoolkit[edgar] @ git+https://github.com/selvaz/LazyTools.git"'
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


def _sgml_field(block: str, name: str) -> str | None:
    """One field of a submission-header ``<DOCUMENT>`` block, ``None`` when
    absent OR empty -- an unlabelled exhibit has no description, and "" would
    read as one it simply left blank."""
    m = re.search(rf"<{name}>([^\r\n<]*)", block, re.I)
    value = m.group(1).strip() if m else ""
    return value or None


def _media_type(filename: str) -> str:
    """Media type from the filename's extension, ``application/octet-stream``
    when it has none we know."""
    lowered = filename.lower()
    for ext, media in _MEDIA_TYPES.items():
        if lowered.endswith(ext):
            return media
    return "application/octet-stream"


def _looks_like_html(filename: str, raw: str) -> bool:
    return filename.lower().endswith((".htm", ".html")) or raw.lstrip().startswith("<")


def _html_to_text(raw: str) -> str:
    """Strip HTML tags to whitespace-normalized plain text (stdlib only)."""
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    return "\n".join(parser.chunks)
