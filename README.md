# LazyTools (`lazytoolkit`)

Reusable **tool providers**, **connector clients**, and **safety wrappers** for
agents built on [LazyBridge](https://github.com/selvaz/LazyBridge) (and
[LazyPulse](https://github.com/selvaz/LazyPulse)).

LazyBridge stays a minimal agent runtime; the concrete, dependency-carrying
tools live here. Anything you add to `Agent(tools=[...])` or
`PulseAgent(tools=[...])` that talks to the outside world belongs in LazyTools.

```
lazybridge       minimal agent runtime — core abstractions only (PyPI)
lazytools        reusable tool providers + connector clients + safety wrappers
lazypulse        always-on orchestration (tick loop, adapters, policy, ledger)
lazycrawler      web crawl & search — surfaced here via the [web] extra
market-data-hub  the single source of financial data — surfaced via datahub_*
lazystats        statistics + HMM/MS regime engines — surfaced via
                 statistical_* and regime_* tools
lazyray          debt-cycle & regime engine on top of market-data-hub
```

## Install

LazyTools is distributed from GitHub (only LazyBridge lives on PyPI). Install
`lazytoolkit` and its extras with a PEP 508 direct reference — this pulls the
current `main`; append `@vX.Y.Z` to the URL to pin a release tag instead:

```bash
G="git+https://github.com/selvaz/LazyTools.git"
pip install "lazytoolkit @ $G"                 # core (just lazybridge)
pip install "lazytoolkit[gmail] @ $G"          # Gmail client + tools
pip install "lazytoolkit[outlook] @ $G"        # Outlook client + tools (Windows desktop, COM)
pip install "lazytoolkit[telegram] @ $G"       # Telegram client + tools
pip install "lazytoolkit[mcp] @ $G"            # Model Context Protocol connector
pip install "lazytoolkit[docs] @ $G"           # PDF/DOCX/HTML document reading
pip install "lazytoolkit[web] @ $G"            # LazyCrawler search/crawl as LLM tools
# Financial data is served by market-data-hub (also GitHub-only); the datahub
# connector needs no extra — install the hub alongside:
#   pip install "market-data-hub @ git+https://github.com/selvaz/market-data-hub.git"
# lazytools.report needs no extra              # deterministic memo rendering
```

> [!IMPORTANT]
> **Compliance & liability — your responsibility.** Several connectors bridge to
> third-party services (e.g. Gmail/Google, Telegram, MCP servers, the external
> tool gateway, Claude Code / Codex). You are solely responsible for ensuring your
> use complies with each provider's terms of service — in particular
> [Google's Terms of Service](https://policies.google.com/terms) and the
> [API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)
> for Gmail, and [Telegram's terms](https://telegram.org/tos/bot-developers) for
> the bot — and with applicable laws. Provided "as is", without warranty; the
> authors accept no liability for how it is used (see [LICENSE](LICENSE)).

## Import contract

```python
from lazytools.connectors.gmail import GmailTools, GmailClient
from lazytools.connectors.outlook import OutlookTools, OutlookClient
from lazytools.connectors.telegram import TelegramTools
from lazytools.connectors.mcp import MCP
from lazytools.connectors.gateway import ExternalToolProvider
from lazytools.connectors.datahub import DataHubTools, MarketDataHubBackend
from lazytools.connectors.regimes import RegimeTools
from lazytools.statistical_analysis import StatisticalAnalysisTools
from lazytools.connectors.web import WebTools
from lazytools.connectors.code_support import claude_code, codex, CodeWriteTools, build_cli_collaboration
from lazytools.report import Memo, Section, TableBlock, render_markdown, render_html
from lazytools.documents import read_docs_tools
from lazytools.skills import build_skill, skill_tools
from lazytools.safety import Allowlist, ConfirmationGate, ActionBlocked
```

## Package layout

| Category | Modules | What lives here |
|---|---|---|
| `connectors/` | `gmail`, `outlook`, `telegram`, `mcp`, `gateway`, `datahub`, `regimes`, `web`, `code_support` | clients + tool providers that bridge to an external service or protocol (incl. the Claude Code / Codex coding CLIs) |
| `statistical_analysis/` | `StatisticalAnalysisTools` | read-only volatility, return-correlation and z-score-outlier analysis backed only by market-data-hub |
| `documents/` | `read_docs` | read documents from a folder/file for LLM consumption |
| `report/` | `models`, `render` | deterministic memo/report rendering (Markdown/HTML) — "LazyReport" |
| `skills/` | `doc_skills` | build/query portable local-documentation skills |
| `safety/` | `allowlist`, `gates`, `urls` | reusable allow-list, one-shot confirmation gate, and SSRF URL guard |
| `testing/` | `fake_clients` | in-memory fakes for the connector Protocols |

**Planned categories** (added when the first module lands, not scaffolded
empty): more connectors (`github`, `slack`, `notion`, `calendar`,
`filesystem`, `browser`) under `connectors/`, and additional reusable base
tools.

## Financial data & reporting

**market-data-hub is the single source of financial data for agents.** All
discovery, resolution, extraction and analysis flow through it — there is no
direct-fetch finance connector on the agent surface.

- **market-data-hub** (`lazytools.connectors.datahub`) — a thin `ToolProvider`
  over the official market-data-hub `tool_*` surface: `datahub_*` discovery,
  instrument resolution, financial facts and coverage tools
  (`datahub_list_datasets`, `datahub_list_symbols`, `datahub_search`,
  `datahub_describe`, `datahub_resolve_instrument`,
  `datahub_get_financial_facts`, `datahub_get_coverage`, …). Raw time-series
  matrices are **off the default surface** — an agent passes ids in and gets
  bounded results out; `DataHubTools(allow_raw_series=True)` adds the capped
  `datahub_get_series` / `datahub_get_returns` for explicit spot-checking, and
  `DataHubTools(allow_refresh=True)` adds the on-demand ingestion write tools
  `datahub_ensure_price_history` / `datahub_ensure_financials`. The
  `MarketDataHubBackend` lazily imports
  `market_data_hub.agent_tools`, so the provider and protocol import without
  market-data-hub installed and a `FakeDataHubBackend` (`lazytools.testing`)
  drives tests offline. market-data-hub is GitHub-only, install it from git
  (`market-data-hub @ git+https://github.com/selvaz/market-data-hub.git`).

  ```python
  from lazytools.connectors.datahub import DataHubTools

  tools = DataHubTools()   # datahub_* discovery + resolution + extraction
  ```

  The low-level transport clients `EdgarClient`
  (`lazytools.connectors.edgar`) and `MarketDataClient`
  (`lazytools.connectors.marketdata`) still ship as injectable plumbing for
  non-agent code, but they are **not** agent tools — agents reach SEC and
  market data through the hub-backed `datahub_*` tools above.

- **Statistical analysis** (`lazytools.statistical_analysis`) — three read-only
  `statistical_return_*` tools that calculate volatility, pairwise return
  correlation and absolute-z-score return outliers from the complete
  `market-data-hub` return history. Outputs are `lazydatacore.AnalysisResult`
  JSON and outliers use `abs(z_score) >= 2` by default. See
  [Statistical analysis](docs/statistical-analysis.md).

  ```python
  from lazytools.statistical_analysis import StatisticalAnalysisTools

  tools = StatisticalAnalysisTools()
  ```

- **Regime detection** (`lazytools.connectors.regimes`) — HMM/Markov-switching
  regime tools from `lazystats.regimes` as `regime_*` LazyBridge tools:
  fitting, state scans, current-regime/changes/summaries, window comparison,
  plot generation, and the SQLite depot + parameter store. Read tools are
  always exposed; fitting/persistence/deletion require
  `RegimeTools(allow_write=True)`. Data loads only through market-data-hub
  (`regime_load_from_datahub`) — no file-path loader on the agent surface.
  Needs `lazystats[regimes]` installed
  (`"lazystats[regimes] @ git+https://github.com/selvaz/LazyStats.git"`).
  See [Regime detection](docs/regimes.md).

  ```python
  from lazytools.connectors.regimes import RegimeTools

  tools = RegimeTools()                  # read-only inspection
  tools = RegimeTools(allow_write=True)  # + load/fit/persist/delete
  ```

- **Report** (`lazytools.report`, no extra — "LazyReport") — deterministic,
  domain-agnostic memo rendering: `Memo`/`Section`/`TableBlock` models plus
  `render_markdown` / `render_html` (same input → identical output, everything
  HTML-escaped). An LLM writes the prose; the layout is a pure function.
  `ReportTools` exposes `render_memo` and `render_memo_html`; PDF rendering is
  deliberately deferred (heavy dependency).

## Web

- **Web** (`lazytools.connectors.web`, extra `[web]`) — surfaces
  [LazyCrawler](https://github.com/selvaz/LazyCrawler) as an **LLM tool
  interface only**. `WebTools` is a thin pass-through over
  `lazycrawler.CrawlerTools.as_tools()` (search/crawl/get-page tools); the
  crawler engine is *not* vendored and no `WebCrawler` class is re-exported.
  `lazycrawler` is imported lazily, so the connector imports without the extra.

  ```python
  from lazytools.connectors.web import WebTools

  tools = WebTools()                       # delegates to lazycrawler.CrawlerTools
  tools = WebTools(name_prefix="web_")      # optionally prefix tool names
  ```

## Safety model

Dangerous tools (e.g. `gmail_send`, `telegram_send_message`, and the coding
CLIs' `claude_code_write` / `codex_write` via `CodeWriteTools` — read-only
`claude_code` / `codex` need no gate) are gated by two
independent, composable primitives in `lazytools.safety`:

- **`Allowlist`** — case-insensitive target allow-list (`None` = allow all).
- **`ConfirmationGate`** — one-shot, target-bound grants. Each grant authorizes
  exactly one action and is consumed on use; a grant may be bound to a scope
  (the running task id) so a concurrent task can never spend it. There is no
  sticky global approval.

A harmless companion is always available alongside the gated action (e.g.
`gmail_create_draft` is never gated; only `gmail_send` is) — the
dry-run-first pattern.

## Dependency rules

`lazytools → lazybridge` is the only allowed dependency. `lazytools` never
imports `lazypulse`, and `lazybridge` never imports `lazytools` (enforced by
boundary tests in both repos).

---

## How This Was Built

LazyBridge is designed by **selvaz** with **Claude Code** and
**ChatGPT Codex** as primary implementation partners.
I focus on architecture, mental model, and trade-offs —
they handle the building under my direction.
