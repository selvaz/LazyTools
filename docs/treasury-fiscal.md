# Treasury Fiscal Data

Three read-only tools over market-data-hub's Treasury Fiscal Data reader
functions: daily cash balances, daily debt totals, and auction results. No
live HTTP call happens here at all -- the hub owns downloading, ingestion,
and storage; this connector only translates the hub's Python-facing reader
API into a shape an LLM tool can call.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.treasury_fiscal import TreasuryFiscalTools

agent = Agent(name="treasury", engine=LLMEngine("deepseek-v4-flash"),
              tools=[TreasuryFiscalTools()])
```

No extra to install and no key to configure -- this is a thin translation
layer, not a network client. It needs the hub's `market_data_hub.reader`
module importable at call time (see **Depends on the hub landing first**
below).

## What it is, and what it is not

**Read-only throughout, by construction, not by policy.** The hub's
ingestion job writes this data; nothing here ever does. There is no write
tool to gate, unlike the messaging connectors -- `allow_write` is simply
accepted and ignored wherever this connector is mounted through the MCP
server, the same convention `econ_calendar` documents for the same reason:
the calendar (and this data) is written by the ingestion job, and a tool
that let an agent edit a published figure would defeat the point of
recording who published it.

**Why these methods restate their arguments instead of wrapping the hub's
readers directly.** Same reasoning as `econ_calendar`: a model cannot supply
an open database connection, and Python types such as `Optional[str]` do not
express absence reliably in a JSON tool schema. Each method binds the
configured database path, accepts plain strings, and translates `""` to "no
filter" before forwarding to exactly one hub reader function. Every method
does its `from market_data_hub.reader import read_treasury_...` import
*inside* the method body rather than at module load time, so importing this
connector never requires the hub reader functions to exist yet -- only
calling a tool does.

## The tools

| Tool | What it does | Required args |
|---|---|---|
| `treasury_cash_balance` | Daily Treasury cash balances in a date range | `start`, `end` |
| `treasury_debt` | Daily Treasury debt totals in a date range -- debt held by the public, intragovernmental holdings, and total public debt outstanding | `start`, `end` |
| `treasury_auctions` | Treasury auction results in a date range, optionally filtered to one security type (`Bill`, `Note`, `Bond`, `TIPS`, `FRN`) | `start`, `end` |

`start`/`end` are required, inclusive, `YYYY-MM-DD` dates on every tool --
each raises `ValueError` if either is missing, before any hub import or
call happens. `account_type` (on `treasury_cash_balance`) and `security_type`
(on `treasury_auctions`) default to `""`, which is translated to `None` --
"no filter" -- before it reaches the hub; the hub never sees an empty
string.

## `account_type` groups several figures per day, not one

`treasury_cash_balance` returns *rows*, not columns: `account_type` is a
label on each row, and a single `record_date` carries several rows, one per
account/figure the source publishes for that day -- opening balance, closing
balance, deposits, withdrawals, and so on all live under different
`account_type` labels on the same date, not different columns of one row.

A caller who wants "the TGA balance on day X" must filter to the exact
account-type label (e.g. `'Treasury General Account (TGA) Opening Balance'`)
*and* the date -- filtering by date alone returns every account_type
published that day mixed together, which is not the same answer and does
not fail loudly. There is no vocabulary-lookup tool here (unlike
`calendar_vocabulary` on the econ calendar) to enumerate valid labels; the
exact strings come from the hub's own source data.

## Through the MCP server

Mounted (once registered) the same way as `econ_calendar` and
`earnings_calendar`: always read-only, so `--allow-unsafe` changes nothing
about it.

## Depends on the market-data-hub `treasury_fiscal` source module

This connector calls `read_treasury_cash_balance`, `read_treasury_debt`, and
`read_treasury_auctions` from `market_data_hub.reader` -- merged into
market-data-hub's `main` (PR #67). An installation pinned to an older
market-data-hub ref won't have these functions yet, and calling any tool
here raises an `ImportError` from that local import; bump the pin to pick
them up. This connector's own tests intercept that import boundary directly
(by injecting a fake `market_data_hub.reader` module) rather than depending
on a live hub install, so they exercise this connector's translation logic
in isolation and do not by themselves prove a given installation's hub pin
is new enough.
