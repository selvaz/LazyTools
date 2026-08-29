# ALFRED

One read-only tool, `alfred_vintage`, over market-data-hub's stored ALFRED
data. ALFRED is FRED's real-time/vintage view of a series: instead of
today's revised figure, it reports what a series *said as of a historical
publication date*. A real example, live-verified against FRED: `CPIAUCSL`
for March 2020 -- first published as `257.953` (as of 2020-04-10), later
revised to `257.989` once more source data came in (as of 2021-02-08), and
revised further since. A plain FRED pull silently substitutes today's fully
revised number for "the" March 2020 value; ALFRED (and this connector) keep
every vintage, each tagged with the date (`as_of`) it was known on.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.alfred import ALFREDTools

agent = Agent(name="macro", engine=LLMEngine("deepseek-v4-flash"),
              tools=[ALFREDTools(db_path="hub.duckdb")])
```

## What it is, and what it is not

**Read-only, and there is no write tool to gate.** `ALFREDTools` does not
accept an `allow_write` argument at all -- unlike the connectors that expose
both read and write tools and gate the write ones behind
`allow_write=True`, there is simply nothing to write here. The hub's own
ingestion job owns downloading and backfilling ALFRED vintages; this
provider only translates an LLM call into a hub reader call and hands back
plain dicts.

**Only what the hub has already backfilled.** `alfred_vintage` never talks
to FRED. If a real, valid series comes back with zero rows, the far more
likely explanation is that the hub's ingestion job has not backfilled that
series into its vintage table yet -- not that the series has no history.
The tool distinguishes the two: an empty result carries a `note` key saying
so explicitly, so a caller does not misread "not backfilled yet" as "this
series has never had a value."

## The four filter combinations

`alfred_vintage(series_id, date="", as_of="")` takes two optional string
filters, both `YYYY-MM-DD`, both translated from `""` to `None` before
reaching the hub:

| `date` | `as_of` | Meaning | Example |
|---|---|---|---|
| unset | unset | Everything stored for the series -- every observation date, at every vintage. | `alfred_vintage("CPIAUCSL")` |
| set | unset | Every vintage of one observation date -- how a single month's figure changed as it was revised. | `alfred_vintage("CPIAUCSL", date="2020-03-01")` -> `257.953`, `257.989`, and every later revision, one row per vintage |
| unset | set | Everything known as of one vintage/realtime date -- a snapshot of the whole series exactly as it stood on one day. | `alfred_vintage("CPIAUCSL", as_of="2020-04-10")` -> the March 2020 row still reads `257.953`, before the 2021 revision |
| set | set | One exact observation at one exact vintage. | `alfred_vintage("CPIAUCSL", date="2020-03-01", as_of="2020-04-10")` -> exactly `257.953` |

`series_id` is required; an empty or whitespace-only value raises
`ValueError` before any hub call is attempted. A `date`/`as_of` combination
that matches no stored vintage returns an empty result with an explanatory
`note`, rather than a bare empty list -- the note's wording depends on
whether a filter was given, since a filtered miss can mean either "this
exact vintage isn't stored" or "this series isn't backfilled at all",
while an unfiltered miss can only mean the latter.

## Why this matters for walk-forward backtests

A backtest that asks "what was CPI for March 2020?" using a plain FRED pull
gets whatever FRED serves *today* -- the fully revised, several-times-since
figure -- even for a simulated decision date in April 2020, when only
`257.953` had ever been published. That is look-ahead bias: the strategy
sees a number nobody could have known at the time. Filtering
`alfred_vintage` by `as_of` (the decision date, or just before it) instead
of by `date` alone reproduces exactly what was publicly known on that day,
which is the only version of the series a walk-forward simulation is
entitled to use.
