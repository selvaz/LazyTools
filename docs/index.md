# LazyTools (`lazytoolkit`)

Reusable **tool providers**, **connector clients**, and **safety wrappers** for
agents built on [LazyBridge](https://github.com/selvaz/LazyBridge) (and
[LazyPulse](https://github.com/selvaz/LazyPulse)).

!!! info "Part of the LazyBridge ecosystem"
    LazyBridge is the stable core runtime; **LazyTools is where capabilities
    live** — anything you add to `Agent(tools=[...])` that talks to the outside
    world. See the [ecosystem overview](https://lazybridge.com/) for
    how the three packages stack.

```bash
pip install lazytoolkit                 # core (just lazybridge)
pip install 'lazytoolkit[gmail]'        # Gmail client + guarded draft/send tools
pip install 'lazytoolkit[telegram]'     # Telegram client + guarded send tool
pip install 'lazytoolkit[mcp]'          # Model Context Protocol connector
pip install 'lazytoolkit[docs]'         # PDF/DOCX/HTML document reading
```

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

## What's in the box

Each tool has its own deep, reference-grade guide — what it does, how it works
internally, every parameter and exposed tool function, runnable examples, the
safety model, and troubleshooting. Start at the [Tools overview](connectors.md).

| Tool | Module | Guide |
|---|---|---|
| **Gmail** | `connectors/gmail` — guarded draft/send + auth-header parsing | [Gmail](gmail.md) |
| **Telegram** | `connectors/telegram` — guarded send tool | [Telegram](telegram.md) |
| **MCP** | `connectors/mcp` — Model Context Protocol connector | [MCP](mcp.md) |
| **External tool gateway** | `connectors/gateway` — remote JSON-HTTP tool registries | [Gateway](gateway.md) |
| **Documents** | `documents/read_docs` — read `.txt/.md/.pdf/.docx/.html` from a folder/file | [Documents](documents.md) |
| **Skills** | `skills/doc_skills` — build/query portable BM25 doc skills | [Skills](skills.md) |
| **Safety** | `safety/{allowlist,gates}` — reusable allow-list + one-shot confirmation gate | [Safety](safety.md) |

**Planned** (added when the first module lands, not scaffolded empty): more
connectors — `github`, `slack`, `notion`, `calendar`, `filesystem`, `browser` —
and additional base tools.

## Dependency rules

`lazytools → lazybridge` is the only allowed dependency. `lazytools` never
imports `lazypulse`, and `lazybridge` never imports `lazytools` — all enforced
by boundary tests.

> **Migrating from lazybridge ≤0.8?** These tools used to live under
> `lazybridge.ext.{mcp,gateway}` / `lazybridge.external_tools.*`. Those
> deprecation shims were **removed in lazybridge 0.9** — import from
> `lazytools.*` instead. The Gmail/Telegram tools also re-export from
> `lazypulse.adapters.*`, which still emits a `DeprecationWarning`.
