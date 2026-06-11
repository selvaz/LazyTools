"""Market-data adapters: a small Protocol plus the free stooq.com backend.

:class:`~lazytools.connectors.marketdata.client.MarketDataClient` talks to a
:class:`MarketDataAdapter`, so price sources are swappable — the free
:class:`StooqAdapter` ships here; paid adapters (FMP, Polygon, …) can be added
later without touching the client or the tools.

All prices are returned as **strings** exactly as the source reported them, so
downstream code can parse them with :class:`decimal.Decimal` and never loses
precision to a ``float`` round-trip.

``httpx`` is imported lazily inside :meth:`StooqAdapter._http_client`, so this
module imports cleanly without the ``marketdata`` extra. The adapter pins its
requests to the stooq hosts, re-validates redirect targets, and hard-caps
every response body.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import quote, urljoin

from lazytools.safety.urls import validate_public_url

#: Hosts the stooq adapter is pinned to; redirects outside this set are refused.
_STOOQ_HOSTS = frozenset({"stooq.com", "www.stooq.com"})

_QUOTE_URL = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
_HISTORY_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"

#: Default hard cap on every response body (bytes) — ~5 MB.
DEFAULT_MAX_RESPONSE_BYTES = 5_000_000
#: Supported history ranges → calendar days back from the most recent row.
RANGE_DAYS: dict[str, int] = {"1m": 31, "3m": 92, "6m": 183, "1y": 366, "5y": 1827}

_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: stooq symbol suffix → trading currency. Anything unknown falls back to USD
#: (US tickers, the primary use case, map to ``.us``).
_SUFFIX_CURRENCY: dict[str, str] = {
    ".us": "USD",
    ".uk": "GBP",
    ".de": "EUR",
    ".fr": "EUR",
    ".it": "EUR",
    ".nl": "EUR",
    ".jp": "JPY",
    ".hk": "HKD",
    ".pl": "PLN",
    ".hu": "HUF",
}


class MarketDataAdapter(Protocol):
    """The seam between :class:`MarketDataClient` and a concrete price source.

    ``quote`` returns ``{"price", "currency", "as_of", "source"}`` (all
    strings); ``history`` returns rows of
    ``{"date", "open", "high", "low", "close", "volume"}`` (all strings),
    already filtered to ``range_`` (one of :data:`RANGE_DAYS`).
    """

    def quote(self, symbol: str) -> dict[str, str]: ...
    def history(self, symbol: str, *, range_: str) -> list[dict[str, str]]: ...


class StooqAdapter:
    """Free, key-less :class:`MarketDataAdapter` backed by stooq.com CSV endpoints.

    US tickers map to stooq's ``{ticker}.us`` convention; a ticker that
    already carries a market suffix (``sap.de``) is passed through unchanged.

    Args:
        http: Optional injected HTTP client (an ``httpx.Client`` or anything
            exposing ``stream(method, url)``). Built lazily when omitted.
        timeout: Request timeout in seconds for the lazily built client.
        max_response_bytes: Hard cap applied to every response body.
    """

    source = "stooq"

    def __init__(
        self,
        *,
        http: Any | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._http = http
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    # ------------------------------------------------------------------ #
    # MarketDataAdapter
    # ------------------------------------------------------------------ #
    def quote(self, symbol: str) -> dict[str, str]:
        sym = _stooq_symbol(symbol)
        text = self._get(_QUOTE_URL.format(symbol=quote(sym, safe="")))
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            raise ValueError(f"stooq returned no quote data for {symbol!r}")
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in rows[0].items()}
        price = row.get("close", "")
        as_of = row.get("date", "")
        if not price or price.upper() == "N/D" or not as_of or as_of.upper() == "N/D":
            raise ValueError(f"stooq has no quote for {symbol!r} (unknown symbol?)")
        return {"price": price, "currency": _currency_for(sym), "as_of": as_of, "source": self.source}

    def history(self, symbol: str, *, range_: str = "1y") -> list[dict[str, str]]:
        if range_ not in RANGE_DAYS:
            raise ValueError(f"invalid range_ {range_!r}; expected one of {sorted(RANGE_DAYS)}")
        sym = _stooq_symbol(symbol)
        text = self._get(_HISTORY_URL.format(symbol=quote(sym, safe="")))
        rows: list[tuple[date, dict[str, str]]] = []
        for raw in csv.DictReader(io.StringIO(text)):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
            try:
                day = date.fromisoformat(row.get("date", ""))
            except ValueError:
                continue  # malformed row — skip rather than abort the series
            ohlc = {field: row.get(field, "") for field in ("open", "high", "low", "close")}
            if any(not value or value.upper() == "N/D" for value in ohlc.values()):
                continue  # incomplete row — skip
            volume = row.get("volume", "")
            if not volume or volume.upper() == "N/D":
                volume = "0"
            rows.append((day, {"date": row["date"], **ohlc, "volume": volume}))
        if not rows:
            return []
        rows.sort(key=lambda item: item[0])
        # Filter by date, anchored to the most recent row (deterministic — no
        # wall-clock dependency; for a live symbol the last row *is* today).
        cutoff = rows[-1][0] - timedelta(days=RANGE_DAYS[range_])
        return [row for day, row in rows if day >= cutoff]

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    def _http_client(self) -> Any:
        if self._http is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover — exercised only without the extra
                raise ImportError(
                    "StooqAdapter requires the 'marketdata' extra. "
                    "Install it with: pip install 'lazytoolkit[marketdata]'"
                ) from exc
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def _get(self, url: str) -> str:
        """GET ``url`` with a size cap and re-validated redirects."""
        http = self._http_client()
        for _ in range(_MAX_REDIRECTS + 1):
            validate_public_url(url, allowed_hosts=_STOOQ_HOSTS)
            with http.stream("GET", url) as resp:
                if resp.status_code in _REDIRECT_STATUSES:
                    location = resp.headers.get("location")
                    if not location:
                        raise RuntimeError(f"stooq redirect from {url} carried no Location header")
                    url = urljoin(url, location)
                    continue
                resp.raise_for_status()
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise RuntimeError(
                            f"stooq response for {url} exceeds max_response_bytes={self._max_response_bytes}"
                        )
                return bytes(body).decode("utf-8", errors="replace")
        raise RuntimeError(f"stooq request to {url} exceeded {_MAX_REDIRECTS} redirects")


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _stooq_symbol(ticker: str) -> str:
    sym = ticker.strip().lower()
    if not sym:
        raise ValueError("ticker must be non-empty")
    return sym if "." in sym else f"{sym}.us"


def _currency_for(symbol: str) -> str:
    dot = symbol.rfind(".")
    suffix = symbol[dot:] if dot != -1 else ""
    return _SUFFIX_CURRENCY.get(suffix, "USD")
