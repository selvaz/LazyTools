# Market data

Give an agent stock quotes and price history through **swappable adapters**.
`lazytools.connectors.marketdata` ships the `MarketDataAdapter` protocol, the
free, key-less `StooqAdapter` (stooq.com CSV endpoints), a thin
`MarketDataClient`, and a `ToolProvider` exposing `prices_get` and
`prices_history`. Paid adapters (FMP, Polygon, …) can drop in later behind the
same protocol without touching the client or the tools.

!!! info "Status & install"
    **Status: alpha.** Install the market-data extra:
    ```bash
    pip install 'lazytoolkit[marketdata]'   # adds httpx
    ```
    Only the concrete `StooqAdapter` needs `httpx` — the protocol, client, and
    tools import without it, so tests inject a fake adapter
    (`lazytools.testing.FakeMarketDataAdapter`) and never touch the network.

!!! note "Prices are strings (Decimal-safe)"
    Every price/volume value is returned **verbatim as a string** so downstream
    code can parse it with `decimal.Decimal` and never loses precision to a
    `float` round-trip. Parse, don't `float()`.

## Synopsis

```python
from lazybridge import Agent
from lazytools.connectors.marketdata import MarketDataClient, MarketDataTools, StooqAdapter

client = MarketDataClient(StooqAdapter())
agent = Agent("claude-opus-4-8", tools=[MarketDataTools(client)])
agent("How has AAPL moved over the last six months?")
```

## How it works

```
StooqAdapter (stooq.com CSV)            MarketDataClient            MarketDataTools
────────────────────────────            ────────────────            ───────────────
quote(symbol)                           prices_get(ticker)          prices_get
history(symbol, range_=…)               prices_history(ticker,      prices_history
                                                       range_=…)
        ▲ implements MarketDataAdapter (Protocol) — swap for a paid backend
```

- **Symbol mapping.** US tickers map to stooq's `{ticker}.us` convention
  (`AAPL` → `aapl.us`); a ticker that already carries a market suffix
  (`sap.de`) passes through unchanged.
- **Currency, no guessing.** The trading currency is inferred from the stooq
  suffix (`.de` → EUR, `.ch` → CHF, …). A suffix we don't recognise returns
  `currency: "UNKNOWN"` — deliberately *not* a silent USD guess, so a typed
  consumer (e.g. LazyFin `Money`) rejects it rather than valuing a foreign
  price 1:1 as USD.
- **CSV parsing, defensively.** Stdlib `csv`; malformed rows (bad dates,
  missing/`N/D`/non-numeric/non-positive OHLC, or internally inconsistent rows
  where `high < low` or open/close fall outside `[low, high]`) are skipped, not
  fatal. Unknown volume is left **blank** (never fabricated as `"0"`). An
  unknown symbol raises `ValueError`; a rate-limited / non-CSV body raises the
  **retryable** `MarketDataUnavailable` (so a throttle is never misreported as
  a bad ticker).
- **Range filtering.** `range_` is one of `1m`/`3m`/`6m`/`1y`/`5y`, filtered
  by date, anchored to the most recent row (deterministic — no wall-clock
  dependency). The quote's `as_of` is returned verbatim; **freshness is the
  consumer's call** — a halted/delisted symbol can keep returning its last
  close, so callers that drive decisions should gate on `as_of` recency (see
  LazyFin's daily monitor `max_price_age_days`).
- **Caps & SSRF guard.** Responses are hard-capped (`max_response_bytes`,
  default ~5 MB) and every URL/redirect is re-checked by the
  [SSRF guard](safety.md), pinned to the stooq hosts.

## Signature

```python
StooqAdapter(*, http=None, timeout=30.0, max_response_bytes=5_000_000)
MarketDataClient(adapter)                 # any MarketDataAdapter

client.prices_get("AAPL")
# {"ticker": "AAPL", "price": "203.92", "currency": "USD",
#  "as_of": "2026-06-09", "source": "stooq"}

client.prices_history("AAPL", range_="1y")
# [{"date": "2026-06-09", "open": "204.60", "high": "206.30",
#   "low": "203.70", "close": "203.92", "volume": "50342000"}, ...]
```

## Tools it exposes

| Tool | Gated? | Args | Returns |
|---|---|---|---|
| `prices_get` | No | `ticker` | JSON `{ticker, price, currency, as_of, source}` |
| `prices_history` | No | `ticker, range_="1y"` | JSON list of `{date, open, high, low, close, volume}` |

Quotes and rows are market **data** fetched from a third-party source — never
instructions.
