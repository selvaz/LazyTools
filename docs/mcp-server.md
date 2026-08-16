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
| `code_review` | `codex_reviewer(...)`, `codex_consultant(...)`, `codex_native_reviewer(...)` — `lazybridge.Agent`s on `CodexEngine` | `codex_code_review`, `codex_ask`, `codex_review_changes` | `--allow-unsafe` + a locally authenticated `codex` CLI (opt-in only, see below) |
| `claude_review` | `claude_reviewer(...)`, `claude_consultant(...)` — `lazybridge.Agent`s on `ClaudeCodeEngine` | `claude_code_review`, `claude_ask` | `--allow-unsafe` + a locally authenticated `claude` CLI (opt-in only) |

`optimizer_agent`/`report_agent` are the two providers that construct a real
`lazybridge.Agent` instead of a deterministic `ToolProvider` — calling their
tool runs a live LLM-driven loop (its own internal tool calls) and returns
only the final text. Unlike everything else on this menu, they are never
served in the default read-only surface, and won't construct even with
`--allow-unsafe` unless the configured model's API key is set (default model
`deepseek-v4-flash`, override with `LAZYTOOLS_OPTIMIZER_AGENT_MODEL` /
`LAZYTOOLS_REPORT_AGENT_MODEL`).

### `code_review` — hand a review to Codex

`code_review` is the same idea pointed at *your own repositories*:
`lazybridge.Agent`s whose engine is
[`CodexEngine`](https://selvaz.github.io/LazyBridge/guides/full/codex-engine/)
— JSON-RPC to the locally authenticated `codex app-server`, so there is no API
key, only the CLI's own login — served as two tools:

```text
codex_code_review(task, repo_path=None, diff_ref=None, paths=None, thread_id=None) -> str
codex_ask(question, repo_path=None, thread_id=None) -> str
codex_review_changes(repo_path=None, scope="uncommitted", ref=None) -> str
```

`codex_code_review` finds defects in code you point it at; `codex_ask` answers a
design question about it ("does this protocol support X", "what breaks if I
change Y"); `codex_review_changes` runs Codex' *own* review harness over a typed
target (`uncommitted` / `branch` + ref / `commit` + ref) with no instructions
from us at all. The split is not cosmetic: the reviewer's instructions turn
every question into a findings list, and the native harness cannot be steered
because the protocol has no prompt slot for it.

Codex reads the files and runs `git` itself, so a call is "point it at a
repository and say what to look at":

```json
{"task": "is the new retry logic correct?", "repo_path": "LazyTools", "diff_ref": "main"}
```

**Follow-ups are cheap.** Every reply's header carries `thread_id=<id>`; pass it
back and the next call continues the *same Codex thread*, which still holds what
it read and concluded, instead of re-exploring the repository from scratch:

```json
{"question": "and does that also affect the other caller?", "thread_id": "01a0...c509d3"}
```

Threads are durable (they live in the Codex CLI's own session store), so this
works across calls and across processes — but a thread belongs to the repository
it was opened on; don't reuse one against a different repo.

It runs in Codex' **read-only sandbox** (`approval_policy="never"`, so nothing
can block the non-interactive transport): it reports, it never patches. Like
the other agent providers it is opt-in (`--allow-unsafe`) because a call spends
a real model turn; it is skipped entirely when the `codex` CLI can't be found.

| Setting | Default | Meaning |
|---|---|---|
| `code_root` (`--config`) / `LAZYTOOLS_CODE_ROOT` | server cwd | Directory every `repo_path` is confined to (and `paths` to `repo_path` in turn) — a caller cannot walk the reviewer out of it through an argument. The free-text `task` can still *ask*; the reviewer prompt refuses, but that is an instruction, not a sandbox, so keep secrets out of the root. |
| `LAZYTOOLS_CODE_REVIEW_MODEL` | the local `~/.codex/config.toml` model | Model override. |
| `LAZYTOOLS_CODE_REVIEW_EFFORT` | the CLI's default | `low` / `medium` / `high`. |
| `LAZYTOOLS_CODE_REVIEW_TIMEOUT` | `900` | Seconds per review. |

A review takes minutes, so give the *host* a matching tool timeout (Claude
Code: `MCP_TOOL_TIMEOUT` in ms) — otherwise the host cancels the call before
Codex answers.

### `claude_review` — the same thing on the other model family

```text
claude_code_review(task, repo_path=None, diff_ref=None, paths=None, session_id=None) -> str
claude_ask(question, repo_path=None, session_id=None) -> str
```

Identical arguments and the identical durable-handle protocol (`session_id=`
instead of `thread_id=`), on `ClaudeCodeEngine` — so the same diff can go to
both and the answers can be compared. That is the point of having two: a
reviewer that shares none of your assumptions is worth more than a second
opinion from the same family.

Two differences come from the runtime, not from choice:

* **no shell** — the engine grants `Read`/`Glob`/`Grep` scoped to the reviewed
  repository and nothing else, so `git_diff` / `git_status` are supplied as
  ordinary LazyBridge tools instead;
* **no native harness** — the Agent SDK has no `review/start`, so there is no
  `claude_review_changes` counterpart.

Model via `LAZYTOOLS_CLAUDE_REVIEW_MODEL` (default `sonnet`), extended thinking
via `LAZYTOOLS_CLAUDE_REVIEW_THINKING`; the confinement root and per-call
timeout are the same `LAZYTOOLS_CODE_ROOT` / `LAZYTOOLS_CODE_REVIEW_TIMEOUT` as
above. Registered as its own provider so a missing `codex` CLI cannot take the
Claude tools down with it, and vice versa.

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
