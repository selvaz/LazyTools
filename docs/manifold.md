# Manifold

Five read-only tools over Manifold Markets' public REST API. Manifold is a
single, simpler surface than Polymarket's Gamma+CLOB split — one base URL,
one client, no order book to query separately — but it needs its own reading
before comparing the two, starting with what a "probability" even means here.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.manifold import ManifoldTools

agent = Agent(name="markets", engine=LLMEngine("deepseek-v4-flash"),
              tools=[ManifoldTools()])
```

Install with the extra: `pip install "lazytoolkit[manifold] @ git+https://github.com/selvaz/LazyTools.git"`.
There is no key to configure — every endpoint used here is public.

## What it is, and what it is not

**Live and stateless.** Nothing is stored, and every tool reaches the network
on every call. As with Polymarket, an answer is **not reproducible** — ask
tomorrow and the odds have moved, with no record of today's, so a figure
quoted in a document cannot be checked afterwards unless the caller kept it.

**Read-only by construction, not by policy.** Placing a bet needs an
authenticated, API-key-bearing write call this connector does not implement
at all — there is no write tool to gate, unlike the messaging connectors.

## Mostly play-money: a different signal than Polymarket

This is the gotcha that matters most if an agent is asked to compare the two
connectors. Manifold is predominantly a **play-money** platform — most
markets settle in Manifold's own in-app currency (mana), not real money, and
even where a market nets out to something redeemable the stakes are not
Polymarket's real-dollar order book. A Manifold `probability` is crowd
forecasting from users with no dollar-denominated skin in the game; a
Polymarket `outcome_prices` figure is a price real traders paid real money
to move. Both are legitimate forecasting signals, but they are not the same
kind of signal, and an agent that averages or directly compares a Manifold
probability against a Polymarket price without saying so is quietly
mixing units. `manifold_probability`'s reply carries no disclaimer beyond
its BINARY/MULTIPLE_CHOICE note for exactly this reason — the caller
composing a cross-platform comparison is the one who needs to know it, not
just the tool's own consumer.

## `answers` never comes from a listing

Manifold's list and search endpoints (`/markets`, `/search-markets`) only
ever populate a top-level `probability`, and only for `BINARY` markets —
`MULTIPLE_CHOICE` markets have no useful top-level probability there at all.
Per-outcome probabilities for a `MULTIPLE_CHOICE` market live in an `answers`
array that is **only** present on the single-market endpoints
(`/market/{id}` and `/slug/{slug}`), which is why `manifold_list_markets`
and `manifold_search_markets` always return `answers: null` — even for a
`MULTIPLE_CHOICE` market — while `manifold_get_market` populates it. A
caller that needs per-outcome odds for a listed market has to make a second
call, to `manifold_get_market` or `manifold_probability`, keyed by the `id`
the listing already gave it.

## No server-side volume/liquidity sort

Unlike Polymarket's `polymarket_list_markets(order=...)`, Manifold's
`/markets` listing endpoint has no ranking parameter at all —
`manifold_list_markets` returns a page ordered by most-recently-updated,
full stop, and this connector does not silently re-sort or filter that page
by `volume`, `volume_24h`, or `total_liquidity` before handing it back. If
an agent wants "the biggest markets right now" it has two real options: use
`manifold_search_markets` with a relevant term (search relevance is the
closest thing to a ranking here) and inspect `volume`/`total_liquidity` on
each result, or sort the raw rows client-side after fetching them — there is
no vendor-side shortcut to ask for.

## The tools

| Tool | What it does |
|---|---|
| `manifold_list_markets` | a page of markets ordered by most-recently-updated (no volume/liquidity sort); `answers` is always `null` here |
| `manifold_search_markets` | full-text search by a required `term`; same shape and same `answers: null` caveat as listing |
| `manifold_get_market` | one full market by exactly one of `market_id` or `slug`; `found=False` rather than an error on a typo; populates `answers` (as does `manifold_probability` below — never `manifold_list_markets`/`manifold_search_markets`) |
| `manifold_probability` | current probability data for one market id — `probability` for `BINARY`, `answers` for `MULTIPLE_CHOICE` |
| `manifold_recent_bets` | recent raw bet records for one market id, passed through unmodified |

### `manifold_get_market`'s id/slug rule

Exactly one of `market_id` or `slug` is required — passing neither or both
raises `ValueError` before any network call:

```python
tools = ManifoldTools()
tools.manifold_get_market(market_id="abc123")           # ok
tools.manifold_get_market(slug="will-it-happen")        # ok
tools.manifold_get_market()                             # ValueError
tools.manifold_get_market(market_id="abc123", slug="x")  # ValueError
```

## Through the MCP server

Mounted as the provider id `manifold`, always read-only — there is no write
tool to gate, so `--allow-unsafe` changes nothing about it.

```bash
lazytools-mcp --providers manifold
```

One environment knob, the same convention as `tradingview`/`polymarket`:
`LAZYTOOLS_MANIFOLD_MAX_CALLS` (default `200`; `0` removes the guard).

## Limits worth knowing before you rely on it

* **Play-money, not a price.** See above — a Manifold probability is a
  crowd-forecast signal from a platform where most markets carry no
  real-money stake, materially different from a Polymarket price. Do not
  present the two as directly comparable numbers without saying so.
* **`answers` is get-only.** `manifold_list_markets`/`manifold_search_markets`
  never populate it, even for `MULTIPLE_CHOICE` markets; call
  `manifold_get_market` or `manifold_probability` for per-outcome odds.
* **No ranking on the listing endpoint.** `manifold_list_markets` is ordered
  by recency only; there is no `order=`/`sort=` parameter like Polymarket's,
  and this connector does not fake one client-side.
* **No history, no series.** These are snapshot tools — a probability a week
  ago, or a time series, is not here. `manifold_recent_bets` is the closest
  thing to a trail, and it is capped and unpaginated.
* **Rate limits are real.** A 429 raises immediately and is never retried; a
  per-client call budget (200 by default) stops a runaway loop regardless.
* **Trading is a different surface entirely.** Placing a bet needs an
  authenticated, API-key-bearing write call; none of that is implemented
  here.

!!! warning "Terms of use — your decision, not this package's"
    Manifold market data are public and keyless, but the data is still
    Manifold Markets', subject to its service policies. This connector sends
    whatever it returns to whichever model provider your agent runs on — a
    transmission to a third party whatever the intent. Establishing that
    your use is permitted is your responsibility; see the compliance note in
    the [tools overview](connectors.md).
