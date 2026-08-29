# CFTC Commitments of Traders

Two read-only tools over market-data-hub's CFTC Commitments of Traders (COT)
positioning data: `cftc_positioning_financial` for financial futures (rates,
FX, equity indices, credit) and `cftc_positioning_commodities` for commodity
futures (energy, metals, agriculture). Each wraps exactly one hub reader and adds no behaviour beyond translating
LLM-shaped arguments into the reader's own and turning the returned DataFrame
into JSON-shaped records.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.cftc_cot import CFTCPositioningTools

agent = Agent(name="positioning", engine=LLMEngine("deepseek-v4-flash"),
              tools=[CFTCPositioningTools(db_path="hub.duckdb")])
```

No extra to install — this connector needs `market-data-hub` on the path (it
is not on PyPI; see the [tools overview](connectors.md)), and specifically the
hub's `cftc_cot` source module and `read_cftc_tff`/`read_cftc_legacy` readers.
If the hub installed on the machine predates that module, the tools raise an
`ImportError` on first call rather than at import time — nothing here checks
for it up front.

## What it is, and what it is not

**Read-only, with no write surface to gate.** market-data-hub's own ingestion
job owns pulling the CFTC's published reports and keeping this data current;
these tools only ever read what is already there. Unlike the connectors that
carry an `allow_write`/`allow_refresh` knob to opt into a mutating tool, there
is nothing of the kind here — there is no write tool in this provider for a
flag to gate, so none is offered.

**Two reports, not one, because they cover different futures and split
traders differently:**

| Tool | Covers | Trader breakdown | Hub reader |
|---|---|---|---|
| `cftc_positioning_financial` | Financial futures only — rates, FX, equity indices, credit | Dealer / Asset Manager / Leveraged Money (the CFTC's Traders in Financial Futures, TFF, categories) | `read_cftc_tff` |
| `cftc_positioning_commodities` | Commodity futures only — energy, metals, agriculture | Commercial / Non-Commercial (the CFTC's Legacy report categories) | `read_cftc_legacy` |

The two reports are not two views of the same data: a financial contract
never appears in the Legacy split and a commodity contract never appears in
the TFF one, and the trader categories themselves differ — TFF's
dealer/asset-manager/leveraged-money breakdown has no equivalent in the
coarser Legacy commercial/non-commercial split. Picking the wrong tool for a
contract does not error, it just returns nothing, which is why each tool's
description names the other as the alternative.

## The tools

Both share the same signature and reply shape:

```python
tools = CFTCPositioningTools(db_path="hub.duckdb")
tools.cftc_positioning_financial("2026-01-01", "2026-08-01", "EURO FX")
tools.cftc_positioning_commodities("2026-01-01", "2026-08-01")  # all commodity contracts
```

| Parameter | Meaning |
|---|---|
| `start` | Inclusive first report date, `YYYY-MM-DD`. Required — an empty string raises `ValueError` before any import or hub call. |
| `end` | Inclusive last report date, `YYYY-MM-DD`. Required, same rule as `start`. |
| `contract_market_name` | Exact CFTC contract market name to filter on, or `""` (the default) for every contract in that report. `""` is translated to `None` before reaching the hub reader — the hub's own optional-argument convention, which an LLM tool schema cannot express directly. |

The reply is a dict:

```python
{
    "start": "2026-01-01",
    "end": "2026-08-01",
    "contract_market_name": "EURO FX",   # or None if no filter was given
    "returned": 34,
    "rows": [...],   # list[dict], one row per report date x contract
}
```

`rows` is the hub DataFrame converted with `.to_dict(orient="records")` — one
plain dict per row, JSON-shaped for a model to read directly. Column names
follow whichever report was queried (dealer/asset-manager/leveraged-money
long/short/spread positions and percent-of-open-interest for TFF; commercial/
non-commercial long/short for Legacy), unchanged from what the hub reader
returns.

## Why these methods restate their arguments

The pattern is the same one `EconCalendarTools` documents: the hub readers'
Python-facing optional parameters use `None` for "no filter", which does not
translate reliably into an LLM tool schema — a model omitting an argument and
a model passing null are not reliably distinguishable downstream. So these
tools take required date strings and `""` for an absent contract filter, and
translate `""` to `None` before calling the hub. They also bind `db_path` at
construction time, since a model cannot supply or manage the underlying
database connection itself.

## Depends on the hub's `cftc_cot` source landing first

This connector wraps two functions —
`market_data_hub.reader.read_cftc_tff`/`read_cftc_legacy` — that live in
market-data-hub's own `cftc_cot` source module. That module is separate,
parallel work in the hub repository; until it has shipped, the local imports
inside each tool method (`from market_data_hub.reader import read_cftc_tff`,
`from market_data_hub.reader import read_cftc_legacy`) will fail on whatever
hub version is actually installed, even though this connector's own code and
tests do not depend on it being present.
