# Connectors, documents & skills

Every connector exposes a `ToolProvider` you drop straight into
`Agent(tools=[...])` (or `PulseAgent(tools=[...])`).

## Gmail

```python
from lazybridge import Agent
from lazytools.connectors.gmail import GmailClient, GmailTools

client = GmailClient.from_credentials(
    credentials_path="credentials.json",
    token_path="token.json",
    scopes=["https://www.googleapis.com/auth/gmail.modify"],
)
# gmail_create_draft is always allowed; gmail_send is gated (allow-list +
# one-shot confirmation). See Safety.
tools = GmailTools(client, allowed_recipients=["teammate@example.com"])
agent = Agent("claude-opus-4-7", tools=[tools])
```

Install: `pip install 'lazytoolkit[gmail]'`.

## Telegram

```python
from lazytools.connectors.telegram import TelegramClient, TelegramTools

client = TelegramClient.from_token("BOT_TOKEN")
# Reply freely to one chat: allow-list it and drop confirmation.
tools = TelegramTools(client, allowed_chat_ids=[123456789], require_confirmation=False)
```

Install: `pip install 'lazytoolkit[telegram]'`.

## MCP (Model Context Protocol)

An `MCPServer` connects to an external MCP server and expands into one
`lazybridge.Tool` per remote tool.

```python
from lazybridge import Agent
from lazytools.connectors.mcp import MCP

fs = MCP.stdio(
    "fs",
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp/project"],
    allow=["fs.read_*"],          # least-privilege filtering
)
agent = Agent("claude-opus-4-7", tools=[fs])
```

Install: `pip install 'lazytoolkit[mcp]'`.

## External tool gateway

A connector to commercial integration gateways (Composio / Pipedream / Arcade)
or any service that publishes tools over JSON HTTP.

```python
from lazytools.connectors.gateway import ExternalToolProvider, JsonHttpExternalToolClient

provider = ExternalToolProvider(JsonHttpExternalToolClient(base_url="https://gateway.example.com"))
```

## Documents

```python
from lazytools.documents import read_docs_tools, read_folder_docs

# As a tool (sandbox to a base directory when exposing to an agent):
tools = read_docs_tools(base_dir="/safe/docs")
# Or call directly:
text = read_folder_docs("/safe/docs", extensions="md,pdf", recursive=True)
```

Install (for PDF/DOCX/HTML): `pip install 'lazytoolkit[docs]'`.

## Skills

Index a documentation folder into a portable BM25 skill bundle, then query it.

```python
from lazytools.skills import build_skill, query_skill, skill_tools

meta = build_skill(["./docs"], "my-project")
brief = query_skill(meta["skill_dir"], "How does auth work?")
tools = skill_tools(skill_dir=meta["skill_dir"])   # expose to an agent
```

No extra dependencies beyond the standard library.
