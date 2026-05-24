# LazyTools (`lazytoolkit`)

Reusable **tool providers**, **connector clients**, and **safety wrappers** for
agents built on [LazyBridge](https://github.com/selvaz/LazyBridge) (and
[LazyPulse](https://github.com/selvaz/LazyPulse)).

!!! info "Part of the LazyBridge ecosystem"
    LazyBridge is the stable core runtime; **LazyTools is where capabilities
    live** — anything you add to `Agent(tools=[...])` that talks to the outside
    world. See the [ecosystem overview](https://lazybridge.com/ecosystem/) for
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

| Category | Modules | Docs |
|---|---|---|
| **Connectors** | `connectors/{gmail,telegram,mcp,gateway}` — clients + tool providers that bridge to an external service or protocol | [Connectors](connectors.md) |
| **Documents** | `documents/read_docs` — read `.txt/.md/.pdf/.docx/.html` from a folder/file | [Connectors](connectors.md#documents) |
| **Skills** | `skills/doc_skills` — build/query portable BM25 doc skills | [Connectors](connectors.md#skills) |
| **Safety** | `safety/{allowlist,gates}` — reusable allow-list + one-shot confirmation gate | [Safety](safety.md) |

**Planned** (added when the first module lands, not scaffolded empty): more
connectors — `github`, `slack`, `notion`, `calendar`, `filesystem`, `browser` —
and additional base tools.

## Dependency rules

`lazytools → lazybridge` is the only allowed dependency. `lazytools` never
imports `lazypulse`, and `lazybridge` never imports `lazytools` — all enforced
by boundary tests.

> **Migrating from lazybridge ≤0.7.9?** These tools used to live under
> `lazybridge.ext.{mcp,gateway}` / `lazybridge.external_tools.*` (and the
> Gmail/Telegram tools under `lazypulse.adapters.*`). The old paths still work
> with a `DeprecationWarning` until 0.9 — import from `lazytools.*` instead.
