# LazyTools (`lazytoolkit`)

LazyTools is the **integration layer for Claude Code + Codex collaboration**.
It connects engine-backed coding tools, detached jobs with durable approval
tickets, shared project evidence and deterministic quota routing. One engine
writes, the other reviews; code runs the checks and records the acceptance.

**LazyTools 0.10.2 is Alpha.** It requires LazyBridge 1.8.1 or later within
1.x. LazyBridge core is Stable; that does not make every integration Stable.
With a subscription login, coding runtimes use your plan's quota; LazyTools
inherits runtime authentication and does not force a billing path.

Start with the [async code bridge](code-bridge.md) and its full
Claude-writes/Codex-reviews toy-repo flow. Use the [MCP code-support tools](code-support/index.md)
for short calls and named native conversations. Keep plans, claims, contracts,
verification and notes in [Projects](projects.md); render the recorded
schedule as a deterministic English Gantt. [Quota routing](code-bridge.md#choose-a-model-from-quota)
explains tier selection and admission. **Benchmark in progress**; no measured
quality, time, quota-efficiency or subscription savings result is claimed.

```sh
python -m pip install "lazytoolkit[engines,mcp] @ git+https://github.com/selvaz/LazyTools.git@v0.10.2"
lazytools-mcp projects code_review claude_review code_write code_sessions --allow-unsafe
```

Configure the code root and persistent project Store using the
[MCP setup](mcp-server.md#collaboration-setup). Coding providers are
explicitly opt-in. `--allow-unsafe` exposes mutating MCP tools without per-call
confirmation; bridge jobs use their own approval-ticket queue.

Connectors, data tools, documents and safety wrappers support the same agent
workflow. They plug into LazyBridge `Agent(tools=[...])` and LazyPulse.

!!! info "Part of the LazyBridge ecosystem"
    LazyBridge is the stable core runtime; **LazyTools is where capabilities
    live** — anything you add to `Agent(tools=[...])` that talks to the outside
    world. See the [ecosystem overview](https://lazybridge.com/) for
    how the three packages stack.

LazyTools is distributed from GitHub (only LazyBridge is on PyPI). Add an
extra to the pinned direct reference:

```bash
G="git+https://github.com/selvaz/LazyTools.git@v0.10.2"
pip install "lazytoolkit @ $G"                 # core (just lazybridge)
pip install "lazytoolkit[gmail] @ $G"          # Gmail client + guarded draft/send tools
pip install "lazytoolkit[telegram] @ $G"       # Telegram client + guarded send tool
pip install "lazytoolkit[mcp] @ $G"            # Model Context Protocol connector
pip install "lazytoolkit[docs] @ $G"           # PDF/DOCX/HTML document reading
pip install "lazytoolkit[charts] @ $G"         # report figures: charts from datahub series
```

## Import contract

```python
from lazytools.connectors.gmail import GmailTools, GmailClient
from lazytools.connectors.telegram import TelegramTools
from lazytools.connectors.mcp import MCP
from lazytools.connectors.gateway import ExternalToolProvider
from lazytools.report import Memo, Section, FigureBlock, ReportTools, ReportFiles, ecosystem_resolvers
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
| **Financial data** | `connectors/datahub` — market-data-hub as the single finance source (`datahub_*`) | [Financial data](datahub.md) |
| **Regime detection** | `connectors/regimes` — `lazystats.regimes` HMM/MS engines as `regime_*` tools | [Regime detection](regimes.md) |
| **Statistical analysis** | `statistical_analysis` — volatility/correlation/outliers plus OLS/Ridge/Lasso regression over any hub series (returns, levels, factors, macro) | [Statistical analysis](statistical-analysis.md) |
| **Web** | `connectors/web` — [LazyCrawler](https://github.com/selvaz/LazyCrawler) search/crawl as LLM tools (extra `[web]`) | — |
| **Documents** | `documents/read_docs` — read `.txt/.md/.pdf/.docx/.html` from a folder/file | [Documents](documents.md) |
| **Skills** | `skills/doc_skills` — build/query portable BM25 doc skills | [Skills](skills.md) |
| **AI councils** | `skills/council` — spontaneous `AgentPool` deliberation, structured voting, quorum, and DeepSeek + Claude subscription preset | [Spontaneous AI councils](skills-council.md) |
| **Report (LazyReport)** | `report` — deterministic `Memo` → Markdown/HTML with embedded figures (charts/images) via artifact refs; core no extra, `chart:` needs `[charts]` | [Report](report.md) |
| **Safety** | `safety/{allowlist,gates}` — reusable allow-list + one-shot confirmation gate | [Safety](safety.md) |

**Planned** (added when the first module lands, not scaffolded empty): more
connectors — `github`, `slack`, `notion`, `calendar`, `filesystem`, `browser` —
and additional base tools.

!!! tip "Core tools"
    Beyond the connectors above, the [Core tools](core-tools.md) section
    documents composition primitives that ship inside the **LazyBridge core**
    itself rather than in `lazytoolkit` — [Planners](planners/index.md) (LLM-built
    pipelines), [Composition sugar](composition/index.md) (`chain` / `parallel`),
    and [Human-in-the-loop](hil/index.md) (`HumanEngine` / `SupervisorEngine`).

## The ecosystem around LazyTools

Everything below installs from GitHub (only `lazybridge` is on PyPI):

| Package | Role | How LazyTools uses it |
|---|---|---|
| [LazyBridge](https://github.com/selvaz/LazyBridge) | Agent runtime (core) | the substrate — every provider here plugs into `Agent(tools=[...])` |
| [LazyPulse](https://github.com/selvaz/LazyPulse) | Always-on orchestration | consumes LazyTools' clients/tools for its adapters (Gmail, Telegram, Outlook) |
| [LazyCrawler](https://github.com/selvaz/LazyCrawler) | Web crawl & search | surfaced as LLM tools via the `[web]` extra (`WebTools`) |
| [market-data-hub](https://github.com/selvaz/market-data-hub) | **The single source of financial data** | `datahub_*` tools (discovery/resolution/facts/extraction) |
| [LazyStats](https://github.com/selvaz/LazyStats) | Statistics + HMM/MS regime engines | `statistical_*` tools and `regime_*` tools (`[regimes]` extra of lazystats) |
| [LazyRay](https://github.com/selvaz/LazyRay) | Debt-cycle & regime engine | reads market-data-hub directly; no LazyTools connector (not an agent surface). Part of the verified combination since `ecosystem-2026.10`. |
| [LazyFin](https://github.com/selvaz/LazyFin) (private) | Portfolio Manager AI — the finance **domain layer** (canonical models, deterministic kernel, scoring, optimizer, PM agents) | consumes LazyTools' connectors and the `datahub_*`/`regime_*` surfaces; `LazyFin → {LazyPulse, LazyCrawler, LazyTools} → LazyBridge`, never the reverse |

## Dependency rules

`lazytools → lazybridge` is the only allowed dependency. `lazytools` never
imports `lazypulse`, and `lazybridge` never imports `lazytools` — all enforced
by boundary tests.

> **Migrating from lazybridge ≤0.8?** These tools used to live under
> `lazybridge.ext.{mcp,gateway}` / `lazybridge.external_tools.*`. Those
> deprecation shims were **removed in lazybridge 0.9** — import from
> `lazytools.*` instead. The Gmail/Telegram tools also re-export from
> `lazypulse.adapters.*`, which still emits a `DeprecationWarning`.
