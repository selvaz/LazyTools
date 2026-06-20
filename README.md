# LazyTools (`lazytoolkit`)

Reusable **tool providers**, **connector clients**, and **safety wrappers** for
agents built on [LazyBridge](https://github.com/selvaz/LazyBridge) (and
[LazyPulse](https://github.com/selvaz/LazyPulse)).

LazyBridge stays a minimal agent runtime; the concrete, dependency-carrying
tools live here. Anything you add to `Agent(tools=[...])` or
`PulseAgent(tools=[...])` that talks to the outside world belongs in LazyTools.

```
lazybridge   minimal agent runtime — core abstractions only
lazytools    reusable tool providers + connector clients + safety wrappers
lazypulse    always-on orchestration (tick loop, adapters, policy, ledger)
```

## Install

```bash
pip install lazytoolkit                 # core (just lazybridge)
pip install 'lazytoolkit[gmail]'        # Gmail client + tools
pip install 'lazytoolkit[outlook]'      # Outlook client + tools (Windows desktop, COM)
pip install 'lazytoolkit[telegram]'     # Telegram client + tools
pip install 'lazytoolkit[mcp]'          # Model Context Protocol connector
pip install 'lazytoolkit[docs]'         # PDF/DOCX/HTML document reading
pip install 'lazytoolkit[edgar]'        # SEC EDGAR filings + XBRL facts
pip install 'lazytoolkit[marketdata]'   # free stock quotes/history (stooq)
pip install 'lazytoolkit[datahub]'      # market-data-hub discovery + extraction
pip install 'lazytoolkit[web]'          # LazyCrawler search/crawl as LLM tools
# lazytools.report needs no extra      # deterministic memo rendering
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
from lazytools.connectors.edgar import EdgarTools, EdgarClient
from lazytools.connectors.marketdata import MarketDataTools, MarketDataClient, StooqAdapter
from lazytools.connectors.datahub import DataHubTools, MarketDataHubBackend
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
| `connectors/` | `gmail`, `outlook`, `telegram`, `mcp`, `gateway`, `edgar`, `marketdata`, `datahub`, `web`, `code_support` | clients + tool providers that bridge to an external service or protocol (incl. the Claude Code / Codex coding CLIs) |
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

Three modules give an agent an official, zero-cost financial data channel and
a deterministic way to write it up:

- **SEC EDGAR** (`lazytools.connectors.edgar`, extra `[edgar]`) — the official,
  free SEC APIs: `edgar_resolve_company` (ticker/name → CIK),
  `edgar_list_filings`, `edgar_get_filing` (primary document as tag-stripped
  text, labelled `content_is_untrusted`), and `edgar_company_facts` (raw XBRL
  JSON). The client *requires* a declared `user_agent` (SEC fair-access
  policy), throttles to ~10 req/s, hard-caps every response body, and
  re-validates redirects against the pinned SEC hosts.

  ```python
  from lazytools.connectors.edgar import EdgarClient, EdgarTools

  client = EdgarClient("Jane Doe jane@example.com")   # declared UA — required
  tools = EdgarTools(client)                          # Agent(tools=[tools])
  ```

- **Market data** (`lazytools.connectors.marketdata`, extra `[marketdata]`) —
  price quotes/history through **swappable adapters** (the free, key-less
  stooq.com adapter ships first; paid backends can drop in behind the same
  `MarketDataAdapter` protocol). Prices are returned as **strings** so
  downstream code can parse them with `Decimal` and never lose precision.

  ```python
  from lazytools.connectors.marketdata import MarketDataClient, MarketDataTools, StooqAdapter

  client = MarketDataClient(StooqAdapter())
  tools = MarketDataTools(client)   # prices_get / prices_history
  ```

- **market-data-hub** (`lazytools.connectors.datahub`, extra `[datahub]`) — a
  thin `ToolProvider` over the official market-data-hub `tool_*` surface,
  exposing 11 `datahub_*` discovery + extraction tools (`datahub_list_datasets`,
  `datahub_list_symbols`, `datahub_search`, `datahub_describe`,
  `datahub_get_series`, `datahub_get_returns`, `datahub_get_coverage`, …). The
  `MarketDataHubBackend` lazily imports `market_data_hub.agent_tools`, so the
  provider and protocol import without the extra and a `FakeDataHubBackend`
  (`lazytools.testing`) drives tests offline.

  ```python
  from lazytools.connectors.datahub import DataHubTools

  tools = DataHubTools()   # datahub_* discovery + time-series extraction
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
