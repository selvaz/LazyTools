# MCP server

The mirror of the [MCP connector](mcp.md). Where the connector turns an
**external** MCP server into `lazybridge.Tool` entries, the MCP *server*
does the opposite: it **exposes LazyTools' own tool providers over MCP** so
any MCP host — Claude Desktop, Claude Code, ChatGPT Codex — can call
`datahub_*`, `statistical_*`, `regime_*` and web search/crawl as native
tools.

> **Status: alpha.** Install: `pip install "lazytoolkit[mcp] @ git+https://github.com/selvaz/LazyTools.git"`.

The bridge is thin because LazyBridge already normalises every tool behind
one abstraction: `tool.definition()` yields the JSON Schema MCP wants for
`inputSchema`, and `await tool.run(**kwargs)` dispatches with argument
validation built in. The whole server is *expand providers → list them →
dispatch calls*.

## Quick start

Run it over **stdio** (the transport MCP hosts launch):

```bash
lazytools-mcp                       # all read-only providers
lazytools-mcp datahub statistical   # a subset (positional provider ids)
python -m lazytools.mcp_server      # equivalent module form
```

Point Claude Desktop / Claude Code at it:

```json
{
  "mcpServers": {
    "lazytools": { "command": "lazytools-mcp" }
  }
}
```

That's it — the host now sees LazyTools' read-only surface as native tools.
Providers whose optional extra is missing are simply skipped, so a bare
`[mcp]` install serves `datahub` + `statistical`; install
`lazystats[regimes]` / the `[web]` extra to light up `regimes` / `web`.

## Providers

`lazytools-mcp` serves the read-only provider menu. Pass ids to select a
subset, or set `LAZYTOOLS_MCP_PROVIDERS=datahub,statistical` in the env.

| id | Provider | Tools | Needs |
|---|---|---|---|
| `registry` | `RegistryTools()` | `registry_status`, `artifact_search`, `artifact_get` (+ `artifact_register` with `--allow-unsafe`) | none (core) |
| `datahub` | `DataHubTools()` | `datahub_*` discovery, resolution, financial facts | `market-data-hub` installed |
| `statistical` | `StatisticalAnalysisTools()` | volatility, correlation, outliers, regression | `market-data-hub` installed |
| `regimes` | `RegimeTools()` | `regime_*` read-only inspection | `lazystats[regimes]` |
| `web` | `WebTools()` | search / crawl / get-page | `[web]` extra |
| `fin` | `PortfolioOptimizationTools()` + `PortfolioTreeTools()` | `portfolio_optimizer_*` (flat node) + `portfolio_tree_*` (multi-node, interoperable with LazyPortfolio's Tree Studio) | `lazyfin`, `lazyportfolio` |
| `optimizer_agent` | `optimizer_specialist(...)` — a `lazybridge.Agent` | one tool, `portfolio-optimizer-specialist(task: str)` | `--allow-unsafe` + `DEEPSEEK_API_KEY` (opt-in only, see below) |
| `report_agent` | `report_specialist(...)` — a `lazybridge.Agent` | one tool, `report-specialist(task: str)` | `--allow-unsafe` + `DEEPSEEK_API_KEY` (opt-in only, see below) |

`optimizer_agent`/`report_agent` are the two providers that construct a real
`lazybridge.Agent` instead of a deterministic `ToolProvider` — calling their
tool runs a live LLM-driven loop (its own internal tool calls) and returns
only the final text. Unlike everything else on this menu, they are never
served in the default read-only surface, and won't construct even with
`--allow-unsafe` unless the configured model's API key is set (default model
`deepseek-v4-flash`, override with `LAZYTOOLS_OPTIMIZER_AGENT_MODEL` /
`LAZYTOOLS_REPORT_AGENT_MODEL`).

## Safety model

The server is **read-only by default**, enforced in two layers:

1. **Provider-level configuration (authoritative).** `default_providers()`
   constructs every provider in its read-only shape — `DataHubTools()`
   without `allow_raw_series` / `allow_refresh`, `RegimeTools()` with
   `allow_write=False`, which never even *emit* their write tools.
2. **A name-based guard (secondary).** `read_only=True` additionally drops
   any tool whose name matches `UNSAFE_TOOL_PATTERNS` (e.g. `*_send`,
   `*_write`, `*_delete`, `*_ensure_*`, `*_fit`) with a logged warning — a
   coarse net for the case where a write-enabled provider is passed in by
   mistake.

There is no interactive `ConfirmationGate` over MCP, so mutating tools stay
off the default surface. To expose them you must opt in explicitly:

* **CLI** — `--allow-unsafe` constructs the providers in write-enabled mode
  (`default_providers(ids, allow_write=True)`) *and* disables the name
  guard, so the menu's writers (datahub refresh/register, regime
  fit/persist/delete) are emitted and served. No gating is applied.
* **Programmatic** — build write-enabled providers yourself and pass
  `read_only=False` to `build_server`, ideally with your own allow-list /
  confirmation wrapper around the mutating tools.

## Programmatic use

```python
import asyncio
from lazytools.mcp_server import build_server, serve_stdio, default_providers

# Read-only by default.
server = build_server(default_providers())
asyncio.run(serve_stdio(server))
```

`build_server` accepts any mix of `ToolProvider`s, `Tool`s, and plain
callables — so you can serve a custom, curated surface:

```python
from lazytools.connectors.datahub import DataHubTools
from lazytools.statistical_analysis import StatisticalAnalysisTools

server = build_server(
    [DataHubTools(), StatisticalAnalysisTools()],
    name="lazytools-finance",
    instructions="Read-only market data + statistics.",
)
```

## API

::: lazytools.mcp_server
    options:
      members:
        - build_server
        - serve_stdio
        - default_providers
        - expand_tools
        - result_to_text
        - PROVIDER_FACTORIES
        - UNSAFE_TOOL_PATTERNS
