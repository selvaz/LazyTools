"""Adapter-backed price client.

:class:`MarketDataClient` is a thin, source-agnostic facade over a
:class:`~lazytools.connectors.marketdata.adapters.MarketDataAdapter` — swap
the free :class:`~lazytools.connectors.marketdata.adapters.StooqAdapter` for a
paid backend without touching callers or tools.

All prices are **strings**, verbatim from the source, so downstream code can
parse them with :class:`decimal.Decimal` and never loses precision to a
``float`` round-trip.
"""

from __future__ import annotations

from lazytools.connectors.marketdata.adapters import RANGE_DAYS, MarketDataAdapter

#: Supported ``range_`` values for :meth:`MarketDataClient.prices_history`.
VALID_RANGES: tuple[str, ...] = tuple(RANGE_DAYS)


class MarketDataClient:
    """Price lookups through a swappable :class:`MarketDataAdapter`."""

    def __init__(self, adapter: MarketDataAdapter) -> None:
        self._adapter = adapter

    def prices_get(self, ticker: str) -> dict[str, str]:
        """Latest quote for a ticker.

        Returns ``{"ticker": "AAPL", "price": "203.92", "currency": "USD",
        "as_of": "2026-06-09", "source": "stooq"}`` — the price is a string
        (Decimal-safe, see module docstring).
        """
        symbol = ticker.strip()
        if not symbol:
            raise ValueError("ticker must be non-empty")
        result = self._adapter.quote(symbol)
        return {
            "ticker": symbol.upper(),
            "price": result["price"],
            "currency": result["currency"],
            "as_of": result["as_of"],
            "source": result["source"],
        }

    def prices_history(self, ticker: str, *, range_: str = "1y") -> list[dict[str, str]]:
        """Daily OHLCV history for a ticker over ``range_``.

        ``range_`` is one of ``"1m"``/``"3m"``/``"6m"``/``"1y"``/``"5y"``,
        filtered client-side by date. Each row is ``{"date", "open", "high",
        "low", "close", "volume"}`` with every value a string (Decimal-safe).
        """
        symbol = ticker.strip()
        if not symbol:
            raise ValueError("ticker must be non-empty")
        if range_ not in VALID_RANGES:
            raise ValueError(f"invalid range_ {range_!r}; expected one of {list(VALID_RANGES)}")
        return self._adapter.history(symbol, range_=range_)
