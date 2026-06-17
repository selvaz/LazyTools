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
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import quote, urljoin

from lazytools.safety.urls import validate_public_url


class MarketDataUnavailable(RuntimeError):
    """The source returned a non-data response (rate-limit, outage, HTML error).

    Distinct from a :class:`ValueError` for an *unknown symbol*: an unknown
    symbol is a permanent client error, whereas this is **retryable** — stooq
    answers ``HTTP 200`` with a throttle/HTML body when it is rate-limiting,
    which must not be misreported as a bad ticker.
    """

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

#: Sentinel currency for a symbol whose trading currency we cannot infer from
#: its stooq suffix. Deliberately **not** a valid ISO-4217 code: downstream
#: code that builds a typed money value (e.g. LazyFin ``Money``) rejects it and
#: fails closed, instead of silently valuing a foreign price 1:1 as USD.
UNKNOWN_CURRENCY = "UNKNOWN"

#: stooq symbol suffix → trading currency. A suffix we don't recognise maps to
#: :data:`UNKNOWN_CURRENCY` (never a silent USD guess); US tickers, the primary
#: use case, carry the ``.us`` suffix and map to USD.
_SUFFIX_CURRENCY: dict[str, str] = {
    ".us": "USD",
    ".uk": "GBP",
    ".de": "EUR",
    ".fr": "EUR",
    ".it": "EUR",
    ".nl": "EUR",
    ".es": "EUR",
    ".pt": "EUR",
    ".be": "EUR",
    ".at": "EUR",
    ".ie": "EUR",
    ".fi": "EUR",
    ".gr": "EUR",
    ".jp": "JPY",
    ".hk": "HKD",
    ".pl": "PLN",
    ".hu": "HUF",
    ".cz": "CZK",
    ".ch": "CHF",
    ".se": "SEK",
    ".dk": "DKK",
    ".no": "NOK",
    ".ca": "CAD",
    ".au": "AUD",
    ".sg": "SGD",
    ".in": "INR",
    ".cn": "CNY",
    ".kr": "KRW",
    ".br": "BRL",
    ".mx": "MXN",
    ".tr": "TRY",
}

#: Columns every stooq CSV body must carry; their absence means the body is not
#: data (a rate-limit / HTML error page), surfaced as :class:`MarketDataUnavailable`.
_REQUIRED_COLUMNS = frozenset({"date", "close"})
_OHLC_FIELDS = ("open", "high", "low", "close")


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
        rows = _read_csv(text)
        if not rows:
            raise ValueError(f"stooq returned no quote data for {symbol!r}")
        row = rows[0]
        price = row.get("close", "")
        as_of = row.get("date", "")
        if not price or price.upper() == "N/D" or not as_of or as_of.upper() == "N/D":
            raise ValueError(f"stooq has no quote for {symbol!r} (unknown symbol?)")
        if _positive_decimal(price) is None:
            raise ValueError(f"stooq returned a non-numeric/non-positive price {price!r} for {symbol!r}")
        return {"price": price, "currency": _currency_for(sym), "as_of": as_of, "source": self.source}

    def history(self, symbol: str, *, range_: str = "1y") -> list[dict[str, str]]:
        if range_ not in RANGE_DAYS:
            raise ValueError(f"invalid range_ {range_!r}; expected one of {sorted(RANGE_DAYS)}")
        sym = _stooq_symbol(symbol)
        text = self._get(_HISTORY_URL.format(symbol=quote(sym, safe="")))
        rows: list[tuple[date, dict[str, str]]] = []
        for row in _read_csv(text):
            try:
                day = date.fromisoformat(row.get("date", ""))
            except ValueError:
                continue  # malformed date — skip rather than abort the series
            ohlc = {field: row.get(field, "") for field in _OHLC_FIELDS}
            parsed = {field: _positive_decimal(value) for field, value in ohlc.items()}
            if any(value is None for value in parsed.values()):
                continue  # missing / N/D / non-numeric / non-positive OHLC — skip
            if not _ohlc_consistent(parsed):
                continue  # high < low, or open/close outside [low, high] — bad row, skip
            volume = row.get("volume", "")
            if not volume or volume.upper() == "N/D" or _nonneg_decimal(volume) is None:
                volume = ""  # unknown / bad volume — leave blank, never fabricate "0"
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
    return _SUFFIX_CURRENCY.get(suffix, UNKNOWN_CURRENCY)


def _read_csv(text: str) -> list[dict[str, str]]:
    """Parse a stooq CSV body into normalised rows (keys/values stripped, keys lower-cased).

    Raises :class:`MarketDataUnavailable` when the body lacks the expected data
    columns — i.e. it is a rate-limit / HTML error page rather than CSV — so a
    throttle is never misreported as an unknown symbol. An empty-but-valid body
    (header only) returns ``[]``.
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
    if not fields >= _REQUIRED_COLUMNS:
        first_line = next((line for line in text.splitlines() if line.strip()), "")
        raise MarketDataUnavailable(
            "stooq returned a non-CSV body (rate-limited or unavailable?); "
            f"first line: {first_line[:120]!r}"
        )
    return [
        {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        for raw in reader
    ]


def _positive_decimal(value: str) -> Decimal | None:
    """Parse ``value`` as a strictly positive, finite :class:`Decimal`, else ``None``."""
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    # Finiteness must be checked *before* the comparison: ``Decimal("NaN")``
    # parses cleanly but ``NaN > 0`` raises ``InvalidOperation``, so a NaN
    # placeholder would otherwise abort the request instead of being dropped.
    return parsed if parsed.is_finite() and parsed > 0 else None


def _nonneg_decimal(value: str) -> Decimal | None:
    """Parse ``value`` as a non-negative, finite :class:`Decimal`, else ``None``."""
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    # Finiteness first — see :func:`_positive_decimal` (NaN comparisons raise).
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _ohlc_consistent(ohlc: dict[str, Decimal | None]) -> bool:
    """True when the four prices satisfy ``low <= open, close <= high``."""
    o, h, low, c = ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"]
    if o is None or h is None or low is None or c is None:
        return False
    return low <= h and low <= o <= h and low <= c <= h
