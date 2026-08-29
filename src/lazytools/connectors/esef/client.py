"""Thin wrapper around filings.xbrl.org, the open index of ESEF filings.

ESEF is the EU's machine-readable annual-report mandate: issuers on a regulated
market tag their consolidated IFRS statements in Inline XBRL. XBRL International
runs ``filings.xbrl.org`` as a public index over those filings, and — usefully —
serves each one's tagged data as **xBRL-JSON**, so the numbers can be read
without parsing the report.

Three limits are structural, and a caller that does not know them will read
silence as absence. All three were measured against the live API on 2026-08-29:

* **Annual only.** ESEF covers the annual financial report. A European half-year
  or quarterly figure is not here at all, whatever the issuer published.
* **Not complete, and it says so.** The repository's own About page states it
  cannot reliably discover filings for **Germany and Ireland**. Siemens AG and
  Volkswagen AG resolve as entities and return zero filings — that is coverage
  failing, not a company that does not report.
* **EU/EEA regulated markets only.** A Swiss issuer such as Nestlé S.A. is not
  in the index at all: the entity 404s rather than returning nothing.

The distinction between those last two matters enough to surface it:
:meth:`ESEFClient.entity` returns ``None`` for a 404 (unknown to the index) and
:meth:`ESEFClient.list_filings` returns ``[]`` for a known entity with nothing
filed. Collapsing them loses the difference between "wrong identifier" and
"this country is not covered".
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol
from urllib.parse import urljoin

from lazytools.safety.urls import validate_public_url

#: The single host this connector talks to; redirects off it are refused.
_ESEF_HOSTS = frozenset({"filings.xbrl.org"})

_BASE = "https://filings.xbrl.org"
_ENTITY_URL = _BASE + "/api/entities/{lei}"
_ENTITY_FILINGS_URL = _BASE + "/api/entities/{lei}/filings"

#: Hard cap on a response body. Generous on measurement, not on principle: one
#: real filing's xBRL-JSON (LVMH's 2024 report) is 5.3 MB, and a larger group's
#: will be more.
DEFAULT_MAX_RESPONSE_BYTES = 30_000_000
#: Minimum spacing between requests. No published rate limit exists; this is
#: ordinary courtesy to a free service run by a non-profit.
DEFAULT_MIN_REQUEST_INTERVAL = 0.2
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class ESEFService(Protocol):
    """The subset of the ESEF index that the evidence layer uses."""

    def entity(self, lei: str) -> dict[str, Any] | None: ...
    def list_filings(self, lei: str) -> list[dict[str, Any]]: ...
    def facts_json(self, json_path: str) -> dict[str, Any]: ...


class ESEFNotFound(LookupError):
    """The index has no such entity or document.

    A ``LookupError`` on purpose: the evidence layer already reads that as "the
    source says this is absent", as distinct from a fault.
    """


class ESEFClient:
    """Production :class:`ESEFService` over ``filings.xbrl.org``.

    Args:
        user_agent: a declared identity. Not demanded by the service, but a
            free public index deserves to know who is calling it.
        http: an injected client exposing ``stream(method, url, headers=...)``.
        timeout: request timeout in seconds for the lazily built client.
        max_response_bytes: hard cap on every response body.
        min_request_interval: minimum spacing between requests; ``0`` disables.
    """

    def __init__(
        self,
        user_agent: str = "lazytools ESEF reader",
        *,
        http: Any | None = None,
        timeout: float = 60.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
    ) -> None:
        self._user_agent = (user_agent or "lazytools ESEF reader").strip()
        self._http = http
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._min_request_interval = min_request_interval
        self._last_request_at: float | None = None

    # ------------------------------------------------------------------ #
    # ESEFService
    # ------------------------------------------------------------------ #
    def entity(self, lei: str) -> dict[str, Any] | None:
        """The index's record for one LEI, or ``None`` when it has none.

        ``None`` means the repository does not know this entity — which for a
        Swiss or other non-EEA issuer is the expected answer, not a lookup that
        went wrong.
        """
        try:
            payload = self._get_json(_ENTITY_URL.format(lei=_clean_lei(lei)))
        except ESEFNotFound:
            return None
        data = payload.get("data") or {}
        attributes = data.get("attributes") or {}
        return {
            "lei": str(attributes.get("identifier") or lei),
            "name": str(attributes.get("name") or ""),
            "id": str(data.get("id") or ""),
        }

    def list_filings(self, lei: str) -> list[dict[str, Any]]:
        """Every filing the index holds for one LEI, newest period first.

        An empty list from a *known* entity is the German/Irish case: the issuer
        reports, the repository could not collect it. Use :meth:`entity` to tell
        that apart from an identifier the index has never seen.
        """
        try:
            payload = self._get_json(_ENTITY_FILINGS_URL.format(lei=_clean_lei(lei)))
        except ESEFNotFound:
            return []
        rows: list[dict[str, Any]] = []
        for item in payload.get("data") or []:
            attributes = item.get("attributes") or {}
            rows.append(
                {
                    "fxo_id": str(attributes.get("fxo_id") or ""),
                    "period_end": str(attributes.get("period_end") or ""),
                    "country": str(attributes.get("country") or ""),
                    "json_url": attributes.get("json_url") or None,
                    "report_url": attributes.get("report_url") or None,
                    "package_url": attributes.get("package_url") or None,
                    "sha256": attributes.get("sha256") or None,
                    # When the REPOSITORY ingested it. Deliberately not renamed
                    # to anything resembling a filing date: the index publishes
                    # no such thing, and a caller ordering versions by this
                    # would be ordering by scraping luck.
                    "date_added": attributes.get("date_added") or None,
                    "error_count": attributes.get("error_count"),
                    "inconsistency_count": attributes.get("inconsistency_count"),
                }
            )
        rows.sort(key=lambda r: r["period_end"], reverse=True)
        return rows

    def facts_json(self, json_path: str) -> dict[str, Any]:
        """Fetch one filing's xBRL-JSON by the ``json_url`` the index gave.

        The path comes from :meth:`list_filings`, never from a caller: it is
        joined onto this connector's own base and re-validated, so a document
        this client fetches is always one the index named.
        """
        if not json_path:
            raise ValueError("facts_json needs the json_url from list_filings")
        return self._get_json(urljoin(_BASE, str(json_path)))

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    def _http_client(self) -> Any:
        if self._http is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - needs the extra absent
                raise ImportError(
                    "ESEFClient requires httpx. Install it with: "
                    'pip install "lazytoolkit[esef] @ git+https://github.com/selvaz/LazyTools.git"'
                ) from exc
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def _sleep(self, seconds: float) -> None:  # seam for tests — never sleep in a suite
        time.sleep(seconds)

    def _throttle(self) -> None:
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
            validate_public_url(url, allowed_hosts=_ESEF_HOSTS)
            self._throttle()
            with http.stream("GET", url, headers=headers) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError(f"ESEF redirect from {url} carried no Location header")
                    url = urljoin(url, location)
                    continue
                if response.status_code == 404:
                    raise ESEFNotFound(f"the ESEF index has nothing at {url}")
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise RuntimeError(
                            f"ESEF response for {url} exceeds "
                            f"max_response_bytes={self._max_response_bytes}"
                        )
                return bytes(body)
        raise RuntimeError(f"ESEF request to {url} exceeded {_MAX_REDIRECTS} redirects")

    def _get_json(self, url: str) -> dict[str, Any]:
        data = json.loads(self._get(url).decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"ESEF response for {url} is not a JSON object")
        return data


def _clean_lei(lei: str) -> str:
    """Validate an LEI's shape before it is put into a URL.

    20 alphanumerics, per ISO 17442. Checked here rather than trusted: the value
    reaches this connector from a name search, and a malformed one belongs in an
    error message, not in a request.
    """
    raw = str(lei).strip().upper()
    if len(raw) != 20 or not raw.isalnum():
        raise ValueError(f"not a well-formed LEI: {lei!r} (expected 20 alphanumerics)")
    return raw


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MIN_REQUEST_INTERVAL",
    "ESEFClient",
    "ESEFNotFound",
    "ESEFService",
]
