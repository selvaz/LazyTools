# Financial data (market-data-hub)

`lazytools.connectors.datahub` is the **single source of financial data** for a
LazyBridge agent. Discovery, instrument resolution, financial facts, coverage
and (opt-in) extraction all flow through
[market-data-hub](https://github.com/selvaz/market-data-hub) — there is no
direct-fetch finance connector on the agent surface.

`DataHubTools` is a thin `ToolProvider` over market-data-hub's `tool_*`
surface, prefixing every tool `datahub_`. The `MarketDataHubBackend` imports
`market_data_hub` lazily, so the provider imports without the hub installed and
a `FakeDataHubBackend` (`lazytools.testing`) drives tests offline.

## The design rule

An agent gets **ids in, bounded results out** — never a raw price or return
matrix through its own context. The default surface is read-only and
bounded-results-only:

- **discovery** — `datahub_list_datasets`, `datahub_list_symbols`,
  `datahub_list_macro`, `datahub_list_countries`, `datahub_list_indicators`,
  `datahub_list_sectors`, `datahub_search`, `datahub_describe`;
- **resolution** — `datahub_resolve_instrument` (human input → listing
  candidates with `instrument_id`), `datahub_register_listing`;
- **facts & summaries** — `datahub_get_financial_facts`,
  `datahub_get_statement`, `datahub_get_price_summary`, `datahub_get_coverage`,
  `datahub_get_financials_coverage`;
- **operational** — `datahub_get_ingestion_health`, `datahub_get_job_status`.

Two opt-in switches widen the surface:

- `DataHubTools(allow_raw_series=True)` adds `datahub_get_series` and
  `datahub_get_returns` (capped at 500 rows) for explicit spot-checking. They
  are off by default because a truncated matrix fed into a calculation would
  silently corrupt a long-window result — statistics read the full matrix
  in-process instead (see [Statistical analysis](statistical-analysis.md)).
- `DataHubTools(allow_refresh=True)` adds the on-demand ingestion **write**
  tools `datahub_ensure_price_history` and `datahub_ensure_financials`.

## Install and add to an agent

market-data-hub is distributed from GitHub (only LazyBridge is on PyPI), so
install both the toolkit and the hub from git:

```bash
G="git+https://github.com/selvaz/LazyTools.git"
pip install "lazytoolkit @ $G"
pip install "market-data-hub @ git+https://github.com/selvaz/market-data-hub.git"
```

```python
from lazybridge import Agent
from lazytools.connectors.datahub import DataHubTools

# Default: read-only, bounded-results-only discovery + resolution + facts.
agent = Agent("claude-opus-4-8", tools=[DataHubTools()])

# Opt into raw (capped) series and on-demand ingestion:
agent = Agent(
    "claude-opus-4-8",
    tools=[DataHubTools(allow_raw_series=True, allow_refresh=True)],
)
```

## Testing offline

`lazytools.testing.FakeDataHubBackend` implements the same protocol without
importing `market_data_hub`, so tests exercise the full `datahub_*` surface
with no DuckDB file and no network:

```python
from lazytools.connectors.datahub import DataHubTools
from lazytools.testing import FakeDataHubBackend

tools = DataHubTools(backend=FakeDataHubBackend())
```
