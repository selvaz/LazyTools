# Tools overview

Every tool in LazyTools is a `ToolProvider` (or a function/`Tool` factory) you drop
straight into `Agent(tools=[...])` (or `PulseAgent(tools=[...])`). Each one has its
own deep guide — what it does, how it works internally, every parameter and
exposed tool function, runnable examples, the safety model, and troubleshooting.
Treat each as its own mini-repository.

| Tool | What it gives an agent | Install | Guide |
|---|---|---|---|
| **Gmail** | A safe Gmail outbox: ungated `gmail_create_draft` + guarded `gmail_send`, plus inbound auth-header verification. | `pip install 'lazytoolkit[gmail]'` | [Gmail](gmail.md) |
| **Telegram** | A guarded Telegram outbox: `telegram_send_message` with allow-list + one-shot confirmation. | `pip install 'lazytoolkit[telegram]'` | [Telegram](telegram.md) |
| **MCP** | Drop an existing Model Context Protocol server's tool catalogue into an agent, deny-by-default. | `pip install 'lazytoolkit[mcp]'` | [MCP](mcp.md) |
| **External tool gateway** | Adapt a remote JSON-HTTP tool registry (Composio / Pipedream / Arcade / internal) into LazyBridge tools. | `pip install lazytoolkit` | [Gateway](gateway.md) |
| **Documents** | Read `.txt/.md/.pdf/.docx/.html` from a file or folder, sandboxed, for LLM consumption. | `pip install 'lazytoolkit[docs]'` | [Documents](documents.md) |
| **Skills** | Index docs into a portable BM25 skill bundle and query it for grounded answers — stdlib only. | `pip install lazytoolkit` | [Skills](skills.md) |

Cross-cutting: the [Safety](safety.md) primitives (`Allowlist`,
`ConfirmationGate`, `ActionBlocked`) are what gate the dangerous outbound tools.

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

=== "Gateway"

    ```python
    from lazytools.connectors.gateway import ExternalToolProvider, JsonHttpExternalToolClient

    provider = ExternalToolProvider(
        JsonHttpExternalToolClient(base_url="https://gateway.example.com"),
    )
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
