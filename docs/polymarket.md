# Polymarket

Five read-only tools over Polymarket's two public REST surfaces: Gamma (the
market/event catalog) for discovery, CLOB (the order book) for live pricing.
Both are documented, public, and need no API key for these endpoints.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.polymarket import PolymarketTools

agent = Agent(name="markets", engine=LLMEngine("deepseek-v4-flash"),
              tools=[PolymarketTools()])
```

Install with the extra: `pip install "lazytoolkit[polymarket] @ git+https://github.com/selvaz/LazyTools.git"`.
There is no key to configure — both endpoints are public.

## What it is, and what it is not

**Live and stateless.** Nothing is stored, and every tool reaches the network
on every call. Two things follow:

* an answer is **not reproducible** — ask tomorrow and the odds have moved,
  with no record of today's, so a figure quoted in a document cannot be
  checked afterwards unless the caller kept it;
* **Gamma's prices lag the live book.** Gamma is the catalog, eventually
  consistent, and its `outcome_prices` update less often than the order book —
  the vendor's own guidance is "use Gamma to find what to trade, CLOB to price
  it." `polymarket_list_markets`/`polymarket_get_market` label their prices as
  last-published for exactly this reason; a caller that needs a current quote
  should call `polymarket_price`/`polymarket_midpoint` instead.

**Read-only by construction, not by policy.** Placing or cancelling an order
needs a wallet-signed request (EIP-712 + L2 API credentials) this connector
does not carry at all — there is no write tool to gate, unlike the messaging
connectors.

## No free-text search, and no name-based category filter either

Gamma's `/markets` endpoint does not support a keyword-search parameter:
passing one is silently ignored and the default ordering comes back instead.
Less obviously, the same is true of a *name*-based category filter —
`tag_slug` looks like it should work (it does, on `/events`) but is silently
ignored on `/markets` too. Verified live 2026-08-28: a query with
`tag_slug=politics` and the identical query without it return the exact same
page. Only the numeric `tag_id` actually narrows `/markets` — a real id
filters, an unknown one correctly returns an empty list rather than being
ignored — which is why `polymarket_list_markets` only exposes `tag_id`.
Look up one known market by its exact, stable slug with
`polymarket_get_market` instead of guessing a search field.

## The tools

| Tool | What it does | Reads |
|---|---|---|
| `polymarket_list_markets` | a ranked page of markets — question, outcomes, last-published prices, volume, the per-outcome `clob_token_ids` needed by every CLOB tool; `offset` pages past the first `limit` rows | Gamma |
| `polymarket_get_market` | one market's full record by its exact slug; `found=False` rather than an error on a typo | Gamma |
| `polymarket_order_book` | the live bids/asks for one outcome token, capped to `depth` levels per side (default 20, max 50) | CLOB |
| `polymarket_price` | the current best bid (`side='buy'`) or best ask (`side='sell'`) for one outcome token | CLOB |
| `polymarket_midpoint` | the book midpoint for one outcome token — cheaper than the full book when only one number is needed | CLOB |

### The slug → token pipeline

Every CLOB call needs a token id, not a market slug — each outcome (Yes, No,
or one candidate in a multi-outcome event) has its own ERC-1155 token and its
own book:

```python
tools = PolymarketTools()
listing = tools.polymarket_list_markets(tag_id=745, limit=5)   # 745 = NBA
token = listing["markets"][0]["clob_token_ids"][0]   # the "Yes" (or first) outcome
tools.polymarket_midpoint(token)
```

Gamma serializes `outcomes`, `outcome_prices` and `clob_token_ids` as
JSON-encoded strings inside its payload, not as arrays — the client decodes
them at the HTTP boundary, so every tool reply already carries plain lists.

## Through the MCP server

Mounted as the provider id `polymarket`, always read-only — there is no write
tool to gate, so `--allow-unsafe` changes nothing about it.

```bash
lazytools-mcp --providers polymarket
```

One environment knob: `LAZYTOOLS_POLYMARKET_MAX_CALLS` (default `200`; `0`
removes the guard).

## Limits worth knowing before you rely on it

* **`polymarket_price`'s `side` names the book, not your trade.** `side='buy'`
  returns the best *bid* (the highest price a buyer is offering) and
  `side='sell'` returns the best *ask* — the opposite of "the price you'd pay
  to buy". Verified live against the vendor 2026-08-28. `polymarket_midpoint`
  sidesteps the question entirely when one number is enough.
* **The order book comes worst-to-best, not best-to-worst.** Bids arrive
  ascending (0.01 → 0.49), asks descending (0.99 → 0.50) — verified live
  2026-08-28. `polymarket_order_book` truncates each side from the *end* of
  the vendor's list to keep the best prices; a naive front-truncation would
  silently return the least useful ones.
* **No history, no series.** These are snapshot tools — a price a week ago, or
  a time series, is not here.
* **Rate limits are real but soft.** Gamma is fronted by Cloudflare with a
  per-IP soft limit; CLOB enforces published per-endpoint limits per IP. A 429
  raises immediately and is never retried; a per-client call budget (200 by
  default) stops a runaway loop regardless.
* **Multi-outcome events are several binary markets, not one.** A 10-candidate
  election event is 10 separate Yes/No token pairs — "the odds" of the event
  is a set of series, not one number.
* **Trading is a different surface entirely.** Order placement needs a
  wallet-signed EIP-712 request and CLOB API credentials derived from it, plus
  standing token approvals; it is subject to geographic restrictions (reads
  work everywhere, new orders are refused from 30+ jurisdictions). None of
  that is implemented here.

!!! warning "Terms of use — your decision, not this package's"
    Gamma and CLOB market data are public and keyless, but the data is still
    Polymarket's, rate-limited per IP. This connector sends whatever it
    returns to whichever model provider your agent runs on — a transmission
    to a third party whatever the intent. Establishing that your use is
    permitted is your responsibility; see the compliance note in the
    [tools overview](connectors.md).
