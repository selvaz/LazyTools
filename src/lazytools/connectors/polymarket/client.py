"""The HTTP layer over Polymarket's two public, read-only REST surfaces.

Gamma (``gamma-api.polymarket.com``) is the catalog: events, markets, slugs,
volumes. CLOB (``clob.polymarket.com``) is the market layer: order books,
best price, midpoint -- keyed by per-outcome ERC-1155 token id, not by
market slug. Both are public and need no API key for these endpoints;
placing or cancelling an order is a different, wallet-authenticated surface
this client deliberately does not implement.

Gamma is eventually consistent and its ``outcomePrices`` lag the live book
by design (the vendor's own guidance: use Gamma to find what to trade, CLOB
to price it) -- callers that need a current price should call
:meth:`PolymarketClient.price` or :meth:`midpoint`, not read ``last_price``
off a market listing.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

USER_AGENT = "LazyTools/polymarket-connector (+https://github.com/selvaz/LazyTools)"

_HTTPX_MISSING = (
    "httpx is required by the Polymarket connector. Install it with the "
    "extra: pip install \"lazytoolkit[polymarket] @ "
    "git+https://github.com/selvaz/LazyTools.git\""
)


class PolymarketError(RuntimeError):
    """A Gamma/CLOB request refused, failed, or answered something unusable."""


class PolymarketBudgetExceeded(PolymarketError):
    """The per-client call budget is spent.

    A guard against a loop -- an agent pricing a hundred token ids one call
    at a time -- rather than against the vendor's own limits, which are
    unpublished for CLOB and only empirically known for Gamma.
    """


@dataclass(frozen=True)
class Market:
    """One Gamma market record, with its JSON-string fields already decoded.

    Gamma serializes ``outcomes``, ``outcomePrices`` and ``clobTokenIds`` as
    JSON *strings* inside the JSON payload, not as arrays -- decoding them at
    the client boundary means every caller downstream sees plain lists and
    cannot forget the step.
    """

    slug: str
    question: str
    active: bool
    closed: bool
    outcomes: list[str]
    outcome_prices: list[str]
    clob_token_ids: list[str]
    volume: float | None
    volume_24hr: float | None
    liquidity: float | None
    end_date: str | None


def _decode_json_field(raw: Any) -> list[Any]:
    """Gamma's ``outcomes``/``outcomePrices``/``clobTokenIds`` shape.

    Each arrives as a JSON-encoded string, e.g. ``'["Yes","No"]'``. A missing
    or malformed field returns ``[]`` rather than raising -- a market with a
    field the vendor renamed should still be listed, just with that one facet
    empty, not drop the whole row.
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


def _to_market(row: dict[str, Any]) -> Market:
    def _num(key: str) -> float | None:
        value = row.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return Market(
        slug=str(row.get("slug") or ""),
        question=str(row.get("question") or ""),
        active=bool(row.get("active")),
        closed=bool(row.get("closed")),
        outcomes=_decode_json_field(row.get("outcomes")),
        outcome_prices=_decode_json_field(row.get("outcomePrices")),
        clob_token_ids=_decode_json_field(row.get("clobTokenIds")),
        volume=_num("volumeNum") if row.get("volumeNum") is not None else _num("volume"),
        volume_24hr=_num("volume24hr"),
        liquidity=_num("liquidityNum") if row.get("liquidityNum") is not None else _num("liquidity"),
        end_date=row.get("endDate"),
    )


class PolymarketClient:
    """A thin, budgeted client over Gamma (catalog) and CLOB (order book).

    Args:
        timeout: seconds per request.
        max_calls: how many requests this client will make before refusing.
            ``None`` removes the guard.
        min_interval: floor on the gap between two requests, with jitter, so
            a burst of tool calls does not arrive as a burst of traffic.
        transport: injected client duck-typed like ``httpx.Client`` (tests
            only).

    Safe to share between threads: a request is claimed under a lock.
    """

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_calls: int | None = 200,
        min_interval: float = 0.05,
        gamma_url: str = GAMMA_URL,
        clob_url: str = CLOB_URL,
        transport: Any = None,
    ) -> None:
        self._timeout = timeout
        self._max_calls = max_calls
        self._min_interval = min_interval
        self._gamma = gamma_url.rstrip("/")
        self._clob = clob_url.rstrip("/")
        self._transport = transport
        self._calls = 0
        self._last_call = 0.0
        self._gate = threading.Lock()

    @property
    def calls_made(self) -> int:
        return self._calls

    def _client(self) -> Any:
        if self._transport is not None:
            return self._transport
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise PolymarketError(_HTTPX_MISSING) from exc
        return httpx.Client(timeout=self._timeout, headers={"User-Agent": USER_AGENT})

    def _reserve(self) -> None:
        with self._gate:
            if self._max_calls is not None and self._calls >= self._max_calls:
                raise PolymarketBudgetExceeded(
                    f"call budget of {self._max_calls} spent. Widen the question rather "
                    f"than iterating one token id at a time."
                )
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.02))
            self._calls += 1
            self._last_call = time.monotonic()

    def _get(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """One GET, retried once on a transient failure.

        Every path out of here is a :class:`PolymarketError`, mirroring the
        TradingView connector's rule that a call and its retry cannot
        silently acquire different error behaviour.
        """
        client = self._client()
        try:
            for attempt in (1, 2):
                self._reserve()
                try:
                    response = client.get(f"{base}{path}", params=params or {})
                except Exception as exc:  # network-level
                    if attempt == 2:
                        raise PolymarketError(f"could not reach {base}{path}: {exc}") from exc
                    time.sleep(0.3 + random.uniform(0, 0.2))
                    continue

                status = getattr(response, "status_code", 0)
                if status == 429:
                    raise PolymarketError(
                        f"{base}{path} answered 429 (rate limited). Stop and retry later, "
                        f"do not loop."
                    )
                if status >= 500 and attempt == 1:
                    time.sleep(0.3 + random.uniform(0, 0.2))
                    continue
                if status == 404:
                    return None
                if status != 200:
                    raise PolymarketError(f"{base}{path} answered HTTP {status}")
                try:
                    return response.json()
                except Exception as exc:
                    raise PolymarketError(f"{base}{path} answered something that is not JSON") from exc
            raise PolymarketError(f"{base}{path} could not be reached")  # pragma: no cover
        finally:
            if self._transport is None:
                client.close()

    # ------------------------------------------------------------------ gamma
    def markets(
        self,
        *,
        closed: bool = False,
        active: bool | None = None,
        tag_id: int | None = None,
        order: str = "volume24hr",
        ascending: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Market]:
        """A page of Gamma markets, sorted server-side.

        There is no free-text search parameter on this endpoint -- passing
        one is silently ignored and the default ordering comes back instead.
        Nor is there a name-based category filter: ``tag_slug`` looks
        plausible (it is real on ``/events``) but is silently ignored here
        too -- verified live 2026-08-28, a query with and without it returns
        an identical page. Only the numeric ``tag_id`` actually narrows
        ``/markets`` (also verified live: a real id filters, an unknown one
        returns an empty list rather than being ignored). Fetch one market
        by ``slug`` (see :meth:`market`) instead of guessing a search field.
        """
        params: dict[str, Any] = {
            "closed": "true" if closed else "false",
            "order": order,
            "ascending": "true" if ascending else "false",
            "limit": max(1, min(int(limit), 500)),
            "offset": max(0, int(offset)),
        }
        if active is not None:
            params["active"] = "true" if active else "false"
        if tag_id is not None:
            params["tag_id"] = tag_id
        payload = self._get(self._gamma, "/markets", params)
        rows = payload if isinstance(payload, list) else []
        return [_to_market(row) for row in rows if isinstance(row, dict)]

    def market(self, slug: str) -> Market | None:
        """One market by its stable slug, or ``None`` if it does not exist."""
        payload = self._get(self._gamma, "/markets", {"slug": slug})
        rows = payload if isinstance(payload, list) else []
        for row in rows:
            if isinstance(row, dict) and row.get("slug") == slug:
                return _to_market(row)
        return None

    # ------------------------------------------------------------------- clob
    def book(self, token_id: str) -> dict[str, Any]:
        """The live order book (bids/asks) for one outcome token."""
        payload = self._get(self._clob, "/book", {"token_id": token_id})
        if not isinstance(payload, dict):
            raise PolymarketError(f"CLOB /book returned no book for token_id {token_id!r}")
        return payload

    def price(self, token_id: str, side: str) -> dict[str, Any]:
        """The best price on one side of the book for one outcome token.

        Args:
            side: ``"buy"`` returns the best bid, ``"sell"`` the best ask --
                verified live against the vendor 2026-08-28: it names the
                book side read, not the trade you would place.
        """
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        payload = self._get(self._clob, "/price", {"token_id": token_id, "side": side})
        if not isinstance(payload, dict):
            raise PolymarketError(f"CLOB /price returned nothing for token_id {token_id!r}")
        return payload

    def midpoint(self, token_id: str) -> dict[str, Any]:
        """The book midpoint for one outcome token."""
        payload = self._get(self._clob, "/midpoint", {"token_id": token_id})
        if not isinstance(payload, dict):
            raise PolymarketError(f"CLOB /midpoint returned nothing for token_id {token_id!r}")
        return payload


__all__ = [
    "PolymarketClient",
    "PolymarketError",
    "PolymarketBudgetExceeded",
    "Market",
    "GAMMA_URL",
    "CLOB_URL",
]
