# Tools overview

Every tool in LazyTools is a `ToolProvider` (or a function/`Tool` factory) you drop
straight into `Agent(tools=[...])` (or `PulseAgent(tools=[...])`). Each one has its
own deep guide — what it does, how it works internally, every parameter and
exposed tool function, runnable examples, the safety model, and troubleshooting.
Treat each as its own mini-repository.

| Tool | What it gives an agent | Install | Guide |
|---|---|---|---|
| **Gmail** | Safe Gmail access: ungated reads (`gmail_list_emails` structured search, `gmail_get_email`) + ungated `gmail_create_draft` + guarded `gmail_send`, plus inbound auth-header verification. | `pip install 'lazytoolkit[gmail]'` | [Gmail](gmail.md) |
| **Outlook** | Safe Outlook access on the user's signed-in Windows desktop (over COM): ungated reads (`outlook_list_emails`, `outlook_get_email`) + ungated `outlook_create_draft` + guarded `outlook_send` — the same allow-list + confirmation model as Gmail. | `pip install 'lazytoolkit[outlook]'` | — |
| **Telegram** | A guarded Telegram outbox: `telegram_send_message` with allow-list + one-shot confirmation. | `pip install 'lazytoolkit[telegram]'` | [Telegram](telegram.md) |
| **MCP** | Drop an existing Model Context Protocol server's tool catalogue into an agent, deny-by-default. | `pip install 'lazytoolkit[mcp]'` | [MCP](mcp.md) |
| **Code Support Agent** | Delegate coding work to Claude Code & Codex — each in CLI or MCP mode, plus a collaboration pipeline. | `pip install lazytoolkit` | [Code Support Agent](code-support/index.md) |
| **External tool gateway** | Adapt a remote JSON-HTTP tool registry (Composio / Pipedream / Arcade / internal) into LazyBridge tools. | `pip install lazytoolkit` | [Gateway](gateway.md) |
| **SEC EDGAR** | Official, free SEC filings + XBRL company facts: resolve companies, list/fetch filings (`content_is_untrusted`), raw facts JSON. | `pip install 'lazytoolkit[edgar]'` | [SEC EDGAR](edgar.md) |
| **Market data** | Stock quotes & OHLCV history via swappable adapters (free stooq backend first); prices as Decimal-safe strings. | `pip install 'lazytoolkit[marketdata]'` | [Market data](marketdata.md) |
| **market-data-hub** | Discovery + extraction over the official market-data-hub: 11 `datahub_*` tools (list/search/describe domains, pull analysis-ready series, returns & coverage). | `pip install 'lazytoolkit[datahub]'` | — |
| **Web** | LazyCrawler's search/crawl/get-page surfaced as LLM tools (interface only — the crawler engine stays standalone). | `pip install 'lazytoolkit[web]'` | — |
| **Documents** | Read `.txt/.md/.pdf/.docx/.html` from a file or folder, sandboxed, for LLM consumption. | `pip install 'lazytoolkit[docs]'` | [Documents](documents.md) |
| **Skills** | Index docs into a portable BM25 skill bundle and query it for grounded answers — stdlib only. | `pip install lazytoolkit` | [Skills](skills.md) |
| **Report (LazyReport)** | Deterministic memo rendering: `Memo` → Markdown/HTML, no LLM, no extra deps. | `pip install lazytoolkit` | [Report](report.md) |

Cross-cutting: the [Safety](safety.md) primitives (`Allowlist`,
`ConfirmationGate`, `ActionBlocked`) are what gate the dangerous outbound tools.

!!! warning "Compliance & liability — your responsibility"
    Several connectors bridge to third-party services (Gmail/Google, Telegram,
    MCP servers, the external tool gateway, Claude Code / Codex). **You are
    solely responsible for ensuring your use complies with each provider's terms
    of service** and with any applicable laws. Automated, bulk, or scheduled
    access can get an account or bot rate-limited or suspended. LazyTools is
    provided **"as is", without warranty, and the authors accept no liability**
    for how it is used (see
    [LICENSE](https://github.com/selvaz/LazyTools/blob/main/LICENSE)). See each
    connector's guide for service-specific notes.

## At a glance

=== "Gmail"

    ```python
    from lazybridge import Agent
    from lazytools.connectors.gmail import GmailClient, GmailTools

    client = GmailClient.from_credentials(
        credentials_path="credentials.json",
        token_path="token.json",
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    # gmail_create_draft is always allowed; gmail_send is gated (allow-list +
    # one-shot confirmation). See Gmail + Safety.
    tools = GmailTools(client, allowed_recipients=["teammate@example.com"])
    agent = Agent("claude-opus-4-8", tools=[tools])
    ```

=== "Outlook"

    ```python
    from lazytools.connectors.outlook import OutlookClient, OutlookTools

    client = OutlookClient()            # attaches to the running Outlook desktop (COM)
    # outlook_create_draft is always allowed; outlook_send is gated (allow-list +
    # one-shot confirmation), mirroring Gmail.
    tools = OutlookTools(client, allowed_recipients=["teammate@example.com"])
    ```

=== "Telegram"

    ```python
    from lazytools.connectors.telegram import TelegramClient, TelegramTools

    client = TelegramClient.from_token("BOT_TOKEN")
    # Reply freely to one chat: allow-list it and drop confirmation.
    tools = TelegramTools(client, allowed_chat_ids=[123456789], require_confirmation=False)
    ```

=== "MCP"

    ```python
    from lazybridge import Agent
    from lazytools.connectors.mcp import MCP

    fs = MCP.stdio(
        "fs",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp/project"],
        allow=["fs.read_*"],          # deny-by-default least-privilege filtering
    )
    agent = Agent("claude-opus-4-8", tools=[fs])
    ```

=== "Code Support Agent"

    ```python
    from lazybridge import Agent, LLMEngine
    from lazytools.connectors.code_support import claude_code, codex, build_cli_collaboration

    # Claude Code and Codex in CLI mode, plus the whole Claude Code + Codex
    # collaboration packaged as a single tool. tool_timeout=None lets each CLI
    # subprocess own its own deadline. (MCP mode: claude_code_mcp / codex_mcp.)
    agent = Agent(
        engine=LLMEngine("claude-opus-4-8", tool_timeout=None),
        tools=[claude_code, codex, build_cli_collaboration()],
    )
    ```

=== "Gateway"

    ```python
    from lazytools.connectors.gateway import ExternalToolProvider, JsonHttpExternalToolClient

    provider = ExternalToolProvider(
        JsonHttpExternalToolClient(base_url="https://gateway.example.com"),
    )
    ```

=== "market-data-hub"

    ```python
    from lazybridge import Agent
    from lazytools.connectors.datahub import DataHubTools

    # 11 datahub_* tools; MarketDataHubBackend imports market_data_hub lazily.
    agent = Agent("claude-opus-4-8", tools=[DataHubTools()])
    ```

=== "Web"

    ```python
    from lazybridge import Agent
    from lazytools.connectors.web import WebTools

    # Thin pass-through over lazycrawler.CrawlerTools — LLM tool interface only.
    agent = Agent("claude-opus-4-8", tools=[WebTools()])
    ```

=== "Documents"

    ```python
    from lazytools.documents import read_docs_tools, read_folder_docs

    # As a tool (sandbox to a base directory when exposing to an agent):
    tools = read_docs_tools(base_dir="/safe/docs")
    # Or call directly from trusted code:
    text = read_folder_docs("/safe/docs", extensions="md,pdf", recursive=True)
    ```

=== "Skills"

    ```python
    from lazytools.skills import build_skill, query_skill, skill_tools

    meta = build_skill(["./docs"], "my-project")
    brief = query_skill(meta["skill_dir"], "How does auth work?")
    tools = skill_tools(skill_dir=meta["skill_dir"])   # expose to an agent
    ```

Follow any guide above for the full, reference-grade treatment.
