"""The HTTP layer over GLEIF's public, read-only LEI REST API.

GLEIF exposes JSON:API records without an API key. This client keeps only the
fields useful to connector callers and deliberately leaves the full vendor
payload at the HTTP boundary. One API asymmetry matters here: a missing direct
or ultimate parent is reported as HTTP 404, while missing children normally
arrive as an empty list. Parent 404s therefore mean "no reported parent", not
a failed lookup; children accept either representation defensively.

A second, subtler asymmetry, verified live: not every 404 on these endpoints
is trustworthy. A real entity with no reported parent answers 404 with
GLEIF's own JSON:API error body; a genuinely nonexistent LEI answers 404 too,
but with an HTML page from the edge/gateway in front of the API -- a
different failure entirely. ``_get(..., allow_404=True)`` only accepts the
former as a confirmed "no relationship"; the latter raises, because it means
the LEI itself was never found.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any

BASE_URL = "https://api.gleif.org/api/v1"

USER_AGENT = "LazyTools/gleif-connector (+https://github.com/selvaz/LazyTools)"

_HTTPX_MISSING = (
    "httpx is required by the GLEIF connector. Install it with the "
    "extra: pip install \"lazytoolkit[gleif] @ "
    "git+https://github.com/selvaz/LazyTools.git\""
)


def _is_gleif_json_error(response: Any) -> bool:
    """Whether a 404 response is GLEIF's own JSON:API error body.

    Verified live against the real API: a real entity with no reported
    parent answers 404 with ``{"errors": [{"status": "404", ...}]}`` from
    GLEIF's own application. A genuinely nonexistent LEI answers 404 too,
    but from the edge/gateway in front of it -- an HTML error page, wrong
    content-type, no ``errors`` key at all. Only the former is a trustworthy
    "no relationship" answer.
    """
    try:
        payload = response.json()
    except Exception:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("errors"), list)


class GLEIFError(RuntimeError):
    """A GLEIF request refused, failed, or answered something unusable."""


class GLEIFBudgetExceeded(GLEIFError):
    """The per-client call budget is spent."""


@dataclass(frozen=True)
class LEIRecord:
    """The useful identity and registration fields from one LEI record."""

    lei: str
    legal_name: str
    status: str
    registration_status: str
    legal_form: str | None
    jurisdiction: str | None
    headquarters_country: str | None
    bic_codes: list[str]
    next_renewal_date: str | None


def _mapping(value: Any) -> dict[str, Any]:
    """Return a mapping-like vendor value as a dict, or an empty dict."""
    return value if isinstance(value, dict) else {}


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _to_record(raw: dict[str, Any]) -> LEIRecord:
    """Convert a JSON:API LEI resource without trusting optional fields."""
    attributes = _mapping(raw.get("attributes"))
    entity = _mapping(attributes.get("entity"))
    registration = _mapping(attributes.get("registration"))
    legal_name = _mapping(entity.get("legalName"))
    legal_address = _mapping(entity.get("legalAddress"))
    headquarters_address = _mapping(entity.get("headquartersAddress"))
    legal_form_value = entity.get("legalForm")
    if isinstance(legal_form_value, dict):
        legal_form = _optional_string(legal_form_value.get("id")) or _optional_string(
            legal_form_value.get("other")
        )
    else:
        legal_form = _optional_string(legal_form_value)
    bic = attributes.get("bic")

    return LEIRecord(
        lei=str(attributes.get("lei") or raw.get("id") or ""),
        legal_name=str(legal_name.get("name") or ""),
        status=str(entity.get("status") or ""),
        registration_status=str(registration.get("status") or ""),
        legal_form=legal_form,
        jurisdiction=_optional_string(legal_address.get("country")),
        headquarters_country=_optional_string(headquarters_address.get("country")),
        bic_codes=[value for value in bic if isinstance(value, str)]
        if isinstance(bic, list)
        else [],
        next_renewal_date=_optional_string(registration.get("nextRenewalDate")),
    )


class GLEIFClient:
    """A thin, budgeted client over GLEIF's public LEI API.

    Args:
        timeout: seconds per request.
        max_calls: how many requests this client will make before refusing.
            ``None`` removes the guard.
        min_interval: floor on the gap between two requests, with jitter.
        base_url: API root, replaceable for tests.
        transport: injected client duck-typed like ``httpx.Client`` (tests
            only).

    Safe to share between threads: each request is claimed under a lock.
    """

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_calls: int | None = 200,
        min_interval: float = 0.05,
        base_url: str = BASE_URL,
        transport: Any = None,
    ) -> None:
        self._timeout = timeout
        self._max_calls = max_calls
        self._min_interval = min_interval
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._calls = 0
        self._last_call = 0.0
        self._gate = threading.Lock()

    @property
    def calls_made(self) -> int:
        """Requests this client has issued, including retries."""
        return self._calls

    def _client(self) -> Any:
        if self._transport is not None:
            return self._transport
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise GLEIFError(_HTTPX_MISSING) from exc
        return httpx.Client(timeout=self._timeout, headers={"User-Agent": USER_AGENT})

    def _reserve(self) -> None:
        with self._gate:
            if self._max_calls is not None and self._calls >= self._max_calls:
                raise GLEIFBudgetExceeded(
                    f"call budget of {self._max_calls} spent. Widen the search rather "
                    f"than looking up one LEI at a time."
                )
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.02))
            self._calls += 1
            self._last_call = time.monotonic()

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_404: bool = False,
        strict_404: bool = False,
    ) -> Any:
        """One GET, retried once on a transient failure.

        ``allow_404=True`` means "a 404 here is a normal, expected outcome,
        return None rather than raising" -- right for ``get_record`` and the
        children endpoints, where any 404 simply means the resource isn't
        there. It is NOT right for the parent-relationship endpoints, where a
        404 is ambiguous: verified live against the real API that a
        genuinely nonexistent LEI and a real entity with no reported parent
        both answer HTTP 404, but with different bodies -- the real "no
        relationship" case answers GLEIF's own JSON:API error shape
        (``{"errors": [...]}``), while a nonexistent LEI answers an HTML 404
        page from the edge/gateway in front of the API (wrong content-type,
        not JSON at all). ``strict_404=True`` (used only by
        direct/ultimate-parent) requires the former shape before accepting a
        404 as a confirmed "no parent"; anything else under a 404 raises,
        because a silent ``has_parent=False`` for a bad LEI is a confidently
        wrong answer, not a helpful one.
        """
        client = self._client()
        url = f"{self._base}{path}"
        try:
            for attempt in (1, 2):
                self._reserve()
                try:
                    response = client.get(url, params=params or {})
                except Exception as exc:  # network-level
                    if attempt == 2:
                        raise GLEIFError(f"could not reach {url}: {exc}") from exc
                    time.sleep(0.3 + random.uniform(0, 0.2))
                    continue

                status = getattr(response, "status_code", 0)
                if status == 429:
                    raise GLEIFError(
                        f"{url} answered 429 (rate limited). Stop and retry later, "
                        f"do not loop."
                    )
                if status >= 500 and attempt == 1:
                    time.sleep(0.3 + random.uniform(0, 0.2))
                    continue
                if status == 404 and allow_404:
                    if strict_404 and not _is_gleif_json_error(response):
                        raise GLEIFError(
                            f"{url} answered 404 without GLEIF's JSON error shape "
                            f"-- this means the LEI itself was not found, not that "
                            f"the parent relationship is empty; check the LEI with "
                            f"get_record() first"
                        )
                    return None
                if status != 200:
                    raise GLEIFError(f"{url} answered HTTP {status}")
                try:
                    payload = response.json()
                except Exception as exc:
                    raise GLEIFError(f"{url} answered something that is not JSON") from exc
                if not isinstance(payload, dict):
                    raise GLEIFError(f"{url} answered a {type(payload).__name__}, not an object")
                return payload
            raise GLEIFError(f"{url} could not be reached")  # pragma: no cover
        finally:
            if self._transport is None:
                client.close()

    def search(
        self,
        name: str,
        *,
        exact_lei: bool = False,
        country: str | None = None,
        limit: int = 20,
    ) -> list[LEIRecord]:
        """Search LEI records by legal name, or by exact LEI code."""
        params: dict[str, Any] = {
            "filter[lei]" if exact_lei else "filter[entity.legalName]": name,
            "page[size]": max(1, min(int(limit), 200)),
            "page[number]": 1,
        }
        if country:
            params["filter[entity.legalAddress.country]"] = country
        payload = self._get("/lei-records", params)
        rows = payload.get("data") if isinstance(payload, dict) else None
        return [_to_record(row) for row in rows or [] if isinstance(row, dict)]

    def get_record(self, lei: str) -> LEIRecord | None:
        """Return one LEI record, or ``None`` when it does not exist.

        Any 404 here is an ordinary "not found" -- no `strict_404`, unlike
        the parent lookups below, because there is no second meaning for a
        404 to be confused with at this endpoint.
        """
        return self._single_record(f"/lei-records/{lei}")

    def direct_parent(self, lei: str) -> LEIRecord | None:
        """Return the reported direct parent; a GLEIF 404 means no parent.

        Raises if the 404 doesn't carry GLEIF's own error body -- that shape
        (not just any 404) is what actually confirms "no parent" rather than
        "this LEI wasn't found at all"; see ``_get``'s docstring.
        """
        return self._single_record(f"/lei-records/{lei}/direct-parent", strict_404=True)

    def ultimate_parent(self, lei: str) -> LEIRecord | None:
        """Return the reported ultimate parent; a GLEIF 404 means no parent.

        Same ``strict_404`` reasoning as :meth:`direct_parent`.
        """
        return self._single_record(f"/lei-records/{lei}/ultimate-parent", strict_404=True)

    def _single_record(self, path: str, *, strict_404: bool = False) -> LEIRecord | None:
        payload = self._get(path, allow_404=True, strict_404=strict_404)
        if payload is None:
            return None
        row = payload.get("data")
        return _to_record(row) if isinstance(row, dict) else None

    def direct_children(self, lei: str, *, limit: int = 50) -> list[LEIRecord]:
        """Return reported direct children, accepting empty data or HTTP 404."""
        return self._children(f"/lei-records/{lei}/direct-children", limit)

    def ultimate_children(self, lei: str, *, limit: int = 50) -> list[LEIRecord]:
        """Return reported ultimate children, accepting empty data or HTTP 404."""
        return self._children(f"/lei-records/{lei}/ultimate-children", limit)

    def _children(self, path: str, limit: int) -> list[LEIRecord]:
        params = {"page[size]": max(1, min(int(limit), 200)), "page[number]": 1}
        payload = self._get(path, params, allow_404=True)
        if payload is None:
            return []
        rows = payload.get("data")
        return [_to_record(row) for row in rows or [] if isinstance(row, dict)]

    def fuzzy_search(self, query: str, *, limit: int = 10) -> list[dict]:
        """Return legal-name completions paired with their resolved LEIs."""
        params = {"field": "entity.legalName", "q": query}
        payload = self._get("/fuzzycompletions", params)
        rows = payload.get("data") if isinstance(payload, dict) else None
        results: list[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            attributes = _mapping(row.get("attributes"))
            relationships = _mapping(row.get("relationships"))
            lei_records = _mapping(relationships.get("lei-records"))
            relationship_data = _mapping(lei_records.get("data"))
            suggestion = attributes.get("value")
            lei = relationship_data.get("id")
            if isinstance(suggestion, str) and isinstance(lei, str):
                results.append({"suggestion": suggestion, "lei": lei})
            if len(results) >= max(1, int(limit)):
                break
        return results


__all__ = [
    "GLEIFClient",
    "GLEIFError",
    "GLEIFBudgetExceeded",
    "LEIRecord",
    "BASE_URL",
]
