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
pip install 'lazytoolkit[telegram]'     # Telegram client + tools
pip install 'lazytoolkit[mcp]'          # Model Context Protocol connector
pip install 'lazytoolkit[docs]'         # PDF/DOCX/HTML document reading
```

> [!IMPORTANT]
> **Compliance & liability — your responsibility.** The Gmail connector accesses
> Gmail via Google's APIs. You are solely responsible for ensuring your use
> complies with [Google's Terms of Service](https://policies.google.com/terms)
> and the [API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
> and with applicable laws. Provided "as is", without warranty; the authors accept
> no liability for how it is used (see [LICENSE](LICENSE)).

## Import contract

```python
from lazytools.connectors.gmail import GmailTools, GmailClient
from lazytools.connectors.telegram import TelegramTools
from lazytools.connectors.mcp import MCP
from lazytools.connectors.gateway import ExternalToolProvider
from lazytools.documents import read_docs_tools
from lazytools.skills import build_skill, skill_tools
from lazytools.safety import Allowlist, ConfirmationGate, ActionBlocked
```

## Package layout

| Category | Modules | What lives here |
|---|---|---|
| `connectors/` | `gmail`, `telegram`, `mcp`, `gateway` | clients + tool providers that bridge to an external service or protocol |
| `documents/` | `read_docs` | read documents from a folder/file for LLM consumption |
| `skills/` | `doc_skills` | build/query portable local-documentation skills |
| `safety/` | `allowlist`, `gates` | reusable allow-list + one-shot confirmation gate for dangerous tools |
| `testing/` | `fake_clients` | in-memory fakes for the connector Protocols |

**Planned categories** (added when the first module lands, not scaffolded
empty): more connectors (`github`, `slack`, `notion`, `calendar`,
`filesystem`, `browser`) under `connectors/`, and additional reusable base
tools.

## Safety model

Dangerous tools (e.g. `gmail_send`, `telegram_send_message`) are gated by two
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
