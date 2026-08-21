# TradingView screener

Six read-only tools over TradingView's public screener endpoint: market breadth,
ranked screens, and per-instrument snapshots of fund, fundamental, technical or
analyst-consensus fields.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.tradingview import TradingViewTools

agent = Agent(name="screen", engine=LLMEngine("deepseek-v4-flash"),
              tools=[TradingViewTools()])
```

Or take the specialist, which ships with the system prompt that goes with the
tools:

```python
from lazytools.skills.screener import screener_analyst

analyst = screener_analyst("deepseek-v4-flash")
print(analyst("How is US market breadth today, and which sector is strongest?").text())
```

Install with the extra: `pip install "lazytoolkit[tradingview] @ git+https://github.com/selvaz/LazyTools.git"`.
There is no key to configure — the endpoint is public.

## What it is, and what it is not

**Live and stateless.** Nothing is stored, and most tools reach the endpoint on
every call — `tradingview_fields` and `tradingview_vocabulary` are the
exceptions, answering from the local catalogue. Two things follow, and the reply
says so rather than leaving them implied:

* an answer is **not reproducible** — ask tomorrow and you get tomorrow's
  numbers, with no record of today's, so a figure quoted in a document cannot be
  checked afterwards unless the caller kept it;
* there is **no series**. Trailing performance figures are there (`perf_1w`
  through `perf_5y`, as they stand right now), but a return between two dates
  you choose, a volatility over your own window, a correlation or a regime are
  not — those live in [market-data-hub](datahub.md), and the specialist's prompt
  tells it to say so rather than approximate them from a snapshot.

## Why the surface is closed

The endpoint exposes 3771 fields and an arbitrary filter language. Passing
either through to a model produces three failures that all look like success:

| The failure | What it looks like | What this connector does |
|---|---|---|
| A guessed field name | the endpoint answers `null`, which reads as a genuine missing value | unknown names raise; only catalogued fields are reachable |
| A filter composed by the model | a plausible row set over a universe nobody declared | screens and breadth metrics are named and pre-defined, and the filter comes back with the number |
| A number without its scale | `0.0945` read as 9.45 % instead of 0.0945 % | every reply carries `units` per field |

A fourth is subtler and is why `tradingview_vocabulary` says *call this first*:
an unknown **value** — a sector spelled another vendor's way — does not raise.
It returns an empty result indistinguishable from a real "nothing matched".

Three vendor fields are withheld outright. `asset_class`, `category` and
`holdings_region` come back as opaque 32-character hashes even with `lang=en`; a
model shown one would treat it as a label. Classification belongs to your own
reference data.

## The tools

| Tool | What it does | Cost |
|---|---|---|
| `tradingview_vocabulary` | every value the other tools accept: markets, bundles, screens, breadth metrics, and the endpoint's own sector/exchange enumerations | free, except `section='enumerations'` |
| `tradingview_fields` | which fields exist, what they mean, in what unit | free (local catalogue) |
| `tradingview_resolve` | plain ticker → `EXCHANGE:SYMBOL`, with collisions reported rather than picked | 1 call |
| `tradingview_quote` | a bundle of fields for up to 60 named instruments | 1 call (+1 if plain tickers need resolving) |
| `tradingview_screen` | a ranked list from a named screen, filter attached | 1 call |
| `tradingview_breadth` | counts over a named universe — the share above a moving average, at new highs, oversold | 2 calls per metric, +1 for the universe |

### Resolution is not optional

Guessing an exchange prefix looks like it works and quietly loses instruments:

```python
tools.tradingview_quote("AMEX:EMB")     # empty — EMB trades on NASDAQ
tools.tradingview_resolve("EMB,INDA")   # {'EMB': 'NASDAQ:EMB', 'INDA': 'CBOE:INDA'}
```

Verified against the 137-symbol market-data-hub universe: 108 of the 109
alphabetic tickers resolve in a single request, with no exchange ambiguity.
Passing plain tickers to `tradingview_quote` resolves them for you.

When a ticker matches several listings the tool **does not choose** unless one
is the primary listing *and* every other candidate carries the same issuer name
and country — and even then it reports the alternatives under `also_listed`.
`is_primary` describes a single listing, not a relationship between listings:
Toyota is primary on TSE and Elm Company is a different issuer trading under the
same `7203` in Riyadh, so the primary flag alone would have picked one company
and filed the other as if it were another venue for the same share.

The rule errs toward refusing. Two genuine listings of one issuer whose names
the vendor spells differently come back as ambiguous rather than merged: a
question you have to answer yourself is cheaper than a confident wrong answer.

The reply also carries `truncated`. When it is true the endpoint matched more
listings than the page read — so an unseen candidate might have made a
"resolved" ticker ambiguous. Nothing is then declared resolved, and the symbols
that never appeared come back under `not_seen` rather than `not_found`: never
having looked is not the same fact as having looked and found nothing.

### Breadth: the universe is the answer

```python
tools.tradingview_breadth(universe="us_cap1b", metrics="above_sma200")
```

The denominator and the exact filter come back with the ratio, because the
choice of universe moves a breadth number more than the market does. Measured on
2026-08-21: *above the 200-day average* was **65.1 %** of US stocks over 1bn and
**44.3 %** of every US stock including OTC microcaps. Both are true. Quoting
either without naming its universe is not.

There are two denominators, and the ratio uses the smaller one. A company listed
three months ago has no 200-day average, so it is not *below* it — it cannot be
asked. Counting it in the denominator would file it under "no", which is why the
reply separates `n_universe` from `n_observable` and reports the difference as
`unmeasurable`. It is not a rounding detail: on the run above, 363 of 4015 names
had no 200-day average, and including them turned 65.1 % into 59.2 %.

The same split makes a renamed vendor field visible. If nothing in the universe
carries the field a metric needs, `n_observable` is zero and the metric returns
no ratio and says why — rather than publishing a confident `0 %`.

## Through the MCP server

Mounted as the provider id `tradingview`, read-only — the endpoint has no write
surface and nothing here persists, so `--allow-unsafe` changes nothing about it.

```bash
lazytools-mcp --providers tradingview
```

Two environment knobs, because a long-lived server is not one agent:
`LAZYTOOLS_TV_MARKET` (default `america`) and `LAZYTOOLS_TV_MAX_CALLS` (default
`500`; `0` removes the guard).

## Limits worth knowing before you rely on it

* **Fund flows are a third-party estimate** over a *trailing* window, not a
  filed figure and not the flow of that day. Consecutive windows overlap, so two
  flow figures are not additive.
* **Ratings and analyst counts are vendor opinion**, not measurement. The
  specialist's prompt permits reporting them as the vendor's view and forbids
  restating them as its own.
* **A renamed field does not raise** — it turns one column entirely null, which
  reads like "no instrument reports this". Every reply carries `non_null` counts
  per field so the two can be told apart.
* **Rate limits are unpublished.** A 429 or 403 raises immediately and is never
  retried; a per-client call budget (200 by default) stops a runaway loop.
* **Price and fundamentals can be in different currencies**, and this is the
  sharpest edge here. On a non-US listing `close` is quoted locally while market
  cap, EPS, revenue and every analyst target come back in USD — the vendor
  normalises them, whatever currency the issuer keeps its books in. Measured on
  2026-08-21: `target_avg / close - 1` gives **−99.2 %** for `TSE:7203` where the
  vendor's converted figure is **+19.5 %**, and **+35.2 %** for `SIX:NESN`
  against a true **+10.0 %**. Use `target_upside_pct` and `pe_ttm`; the reply
  carries a `warning` whenever a row mixes the two.
* **One lookup reaches one market.** `TSE:7203` asked of `america` comes back
  under `not_found`, which means "not on this endpoint", not "no such
  instrument" — the reply says so and points at `market='global'`.

!!! warning "Terms of use — your decision, not this package's"
    The endpoint is undocumented and the data is licensed to TradingView by the
    exchanges. Beyond reading it, this connector **automates** access and sends
    the values to whichever model provider your agent runs on — which is a
    transmission to a third party whatever the intent. Establishing that your
    use is permitted is your responsibility; see the compliance note in the
    [tools overview](connectors.md).
