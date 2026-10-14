# ALFRED

Two read-only tools over ALFRED, FRED's real-time (vintage) view. Instead of
today's revised figure, ALFRED reports what a series *said as of a historical
publication date*.

A real example, verified live against FRED: `CPIAUCSL` for March 2020 was
first published as `257.953` (vintage 2020-04-10), then revised to `257.989`
(vintage 2021-02-08), and revised again since. A plain FRED pull silently
substitutes today's fully revised number for "the" March 2020 value. ALFRED
keeps every vintage, each tagged with the date it was known on.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.alfred import ALFREDTools

agent = Agent(name="macro", engine=LLMEngine("deepseek-v4-flash"),
              tools=[ALFREDTools()])
```

## The two tools

| Tool | Answers |
|---|---|
| `alfred_vintage(series_id, as_of, start="", end="")` | What was this series publishing on `as_of`? |
| `alfred_vintage_dates(series_id, limit=200)` | Which vintages exist at all, newest first? |

`as_of` is **required** on `alfred_vintage`. Defaulting it to today would
return revised data through a tool whose whole purpose is not to — the call
is refused instead, before any request goes out.

## Live, not warehoused

This connector calls FRED at request time and stores nothing. That is a
deliberate change from its first shape, which read a table market-data-hub
ingested.

Vintage data is asked for one series and one date at a time, when a backtest
reaches a decision point. It is not a series anyone sweeps daily. Ingesting
it would mean choosing in advance which series and which vintages might
someday be wanted — and being wrong about that is invisible until the
question is asked and the answer is missing. Asking at call time has no such
guess in it.

The trade is the usual one for a live connector: it needs the network and a
key, and it is not available to a job running offline.

## Credential

Needs a free FRED API key in `FRED_API_KEY` — the same variable
market-data-hub already resolves, deliberately not a second name for one
credential. Keys are free from
[fredaccount.stlouisfed.org](https://fredaccount.stlouisfed.org/apikeys).

Resolution is lazy: importing the module and constructing the provider never
require the key, so the MCP server mounts the provider either way. Only a
call fails, and it fails with an instruction rather than a `KeyError`.

The key is never written into an error message. FRED reports a bad series
id, an impossible date and an invalid key alike as HTTP 400 with an
explanatory body; that body is passed through because it is the useful part,
but the query string — which carries the key — is not. A test pins this.

## Two failure modes worth knowing

**A vintage older than the archive.** ALFRED's vintage history for a series
starts later than the series itself. `CPIAUCSL` has observations from 1947
but only 668 vintages, the oldest being 1972-07-21. Asking for a vintage
before that returns HTTP 400, and FRED's own advice in the body is to
*remove* `realtime_start` — which would turn a point-in-time read into a
revised-data read, precisely the mistake this connector exists to prevent.
That advice is therefore replaced with a pointer to `alfred_vintage_dates`.

**A truncated answer that looks complete.** `alfred_vintage_dates` returns
the vendor's own total alongside the dates, and flags the reply as truncated
when the two differ. A partial list that looks whole is how a caller
concludes a series has a short history. Observations truncate at 400,
keeping the **newest** — a vintage read is almost always asked backwards
from a decision date, so the recent end carries the answer.

## Why this matters for walk-forward backtests

A backtest asking "what was CPI for March 2020?" through a plain FRED pull
gets whatever FRED serves *today*, even for a simulated decision date in
April 2020 when only `257.953` had ever been published. That is look-ahead
bias: the strategy sees a number nobody could have known. Pinning `as_of` to
the decision date reproduces exactly what was public that day, which is the
only version of the series a walk-forward simulation is entitled to use.
