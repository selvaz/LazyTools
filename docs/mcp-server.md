# MCP server

Expose LazyTools' project and coding tools to Claude Code, Codex or another
MCP host. Engine-backed write/review tools are synchronous; detached work and
durable approval tickets belong to [the async code bridge](code-bridge.md).
Other connectors support data, reports and communication. The [MCP connector](mcp.md)
is the reverse direction: it imports another server's tools into an agent.

**LazyTools 0.10.5 is Alpha.** Install the coding runtime dependencies too:

```sh
python -m pip install "lazytoolkit[engines,mcp] @ git+https://github.com/selvaz/LazyTools.git@v0.10.5"
```

## Collaboration setup

From your workspace root, save `lazytools.json` with local paths:

```json
{"code_root": ".", "projects_store_db": "projects.sqlite"}
```

Then launch the same provider combination linked from the homepage:

```sh
lazytools-mcp projects code_review claude_review code_write code_sessions --allow-unsafe --config lazytools.json
```

`code_root` confines repository/cwd arguments; Claude Bash is not path-confined.
`projects_store_db` makes state durable; without config or the Store env vars,
Projects uses memory. Core project tools are on by default. For contracts and
verification tools, set `LAZYTOOLS_PROJECTS_TOOLS=full` in the server environment.
Sign in to the local coding runtimes; with a subscription login their turns use
plan quota. Authentication can also follow another runtime-configured billing
path. Coding providers require explicit selection and `--allow-unsafe`.

A stdio host can launch the server using this configuration (the host's working
directory must be the intended workspace, or replace the config with an absolute
path). The executable must be available in that host's environment:

```json
{
  "mcpServers": {
    "lazytools": {
      "command": "lazytools-mcp",
      "args": ["projects", "code_review", "claude_review", "code_write", "code_sessions",
               "--allow-unsafe", "--config", "lazytools.json"],
      "env": {"LAZYTOOLS_PROJECTS_TOOLS": "full"}
    }
  }
}
```

`--allow-unsafe` enables writes without an interactive confirmation gate. It
is separate from the bridge's durable approvals. Select only the providers
needed. MCP calls block for the delegated turn; configure host timeouts or
use detached bridge jobs. See [code support](code-support/index.md) for
Claude-writes/Codex-reviews calls and `session_name` aliases.

For a persistent server, `--http --host 127.0.0.1 --port 8787` serves Streamable
HTTP at `/mcp`; a compatible host uses a `type: "http"` entry and that URL.
The default transport is stdio and logs go to stderr.

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
Providers whose optional extra is missing are simply skipped, a bare
`[mcp]` install serves available core providers such as registry/projects/report.
`datahub` and `statistical` additionally need the separately installed hub; install
`lazystats[regimes]` / the `[web]` extra to light up `regimes` / `web`.

## Providers

`lazytools-mcp` serves the available read-only providers by default. Coding
and specialist-agent providers require explicit selection and opt-in. Pass ids to select a
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
| `projects` | `ProjectsTools()` | records, owner, plans, schedule/Gantt, quota, notes, cost; full profile adds contracts/verification | configured Store for persistence; writes need `--allow-unsafe` |
| `code_write` | `CodeWriteTools(...)` | `codex_write`, `claude_code_write` | `--allow-unsafe`, local runtimes; Claude needs `[engines]` |
| `code_sessions` | `CodeSessionTools(...)` | list/bind/rename/forget native aliases | explicit opt-in with `--allow-unsafe` |
| `code_review` | `codex_reviewer(...)`, `codex_consultant(...)`, `codex_native_reviewer(...)` — Tools that delegate through `CodexEngine` | `codex_code_review`, `codex_ask`, `codex_review_changes` | `--allow-unsafe` + a locally authenticated `codex` CLI (opt-in only, see below) |
| `claude_review` | `claude_reviewer(...)`, `claude_consultant(...)` — Tools that delegate through `ClaudeCodeEngine` | `claude_code_review`, `claude_ask` | `--allow-unsafe` + a locally authenticated `claude` CLI (opt-in only) |

### `web` — keep interactive research out of the production news DB

By default the `web` provider uses `LAZYCRAWLER_NEWS_DB` for everything,
**read and write**: this is what scheduled production agents rely on, and it is
unchanged when the variable below is unset. An *interactive* session (Claude
Code through the workspace `.mcp.json`, one-off research) should not share that
store: its pages land in the same `pages`/`sessions` tables with nothing marking
them as interactive, and its writes contend with the scheduled crawl's lock
(`database is locked`). Set `LAZYTOOLS_WEB_CACHE_DB` to isolate it:

| `LAZYTOOLS_WEB_CACHE_DB` | Effect |
|---|---|
| unset | Today's behaviour: the crawler reads and writes `LAZYCRAWLER_NEWS_DB`. |
| a file path | `web_search`, `web_crawl`, `web_crawl_many` write to that SQLite file (created on demand, with its directory; safe to delete). |
| empty or `default` | Same, at `~/.lazytools/web_cache.sqlite` (`LAZYTOOLS_DATA_DIR` replaces `~/.lazytools`). |
| `none` (or `:memory:`) | Nothing on disk: pages live in process memory and vanish on exit. |

When it is set, `LAZYCRAWLER_NEWS_DB` (if it names an existing file) is opened
**read-only** (SQLite `mode=ro`: no writes, no WAL pragma, no migrations) and the
read tools merge the two stores. There is no write-through and no copying:

| Tool | Reads |
|---|---|
| `get_page` | the interactive row if present, else the production row |
| `get_session_pages`, `get_crawl_graph` | the interactive store if it has rows for that session, else production |
| `get_artifacts`, `get_rag_document` | the one store that owns the page (or session): interactive if it holds it, even with no artifacts, else production. A newer interactive copy of a URL never inherits the production copy's tables or images |
| `search_cached` | interactive hits first, then production hits, de-duplicated by page, cut to `limit` |
| `get_cache_stats` | `{"interactive": {...}, "production": {...} or null}` instead of flat counts |

The fetch tools do not reuse production pages as a fetch cache (they only know
the interactive store), so a URL already in `news.db` is fetched again.

**Guard.** If the cache path is the same file as `LAZYCRAWLER_NEWS_DB` (or its
`-wal`/`-shm`/`-journal` side files), or lies anywhere under
`C:\ProgramData\InvestmentCommittee`, the provider raises `WebCachePathError`
and the server refuses to start: this error is not downgraded to a skipped
provider. The guard only runs when `LAZYTOOLS_WEB_CACHE_DB` is set.

Enable it for interactive use by adding the variable to the workspace
`.mcp.json` server `env` (never to the production project.toml env):

```json
{
  "mcpServers": {
    "lazytools": {
      "command": "lazytools-mcp",
      "args": ["web"],
      "env": {
        "LAZYCRAWLER_NEWS_DB": "C:\\ProgramData\\InvestmentCommittee\\live\\db\\news.db",
        "LAZYTOOLS_WEB_CACHE_DB": "default"
      }
    }
  }
}
```

`.mcp.json` variables relevant to `web`: `LAZYCRAWLER_NEWS_DB` (production store,
read-only once isolation is on), `LAZYTOOLS_WEB_CACHE_DB` (this section),
`LAZYTOOLS_WEB_MODEL` (smart-mode extraction model, default `deepseek-v4-flash`),
`LAZYTOOLS_DATA_DIR` (base of the default cache path). `--config` accepts
`web_cache_db` (same values) and `news_db_path` for the same two settings.

`optimizer_agent`/`report_agent` are two providers that construct a real
`lazybridge.Agent` instead of a deterministic `ToolProvider` — calling their
tool runs a live LLM-driven loop (its own internal tool calls) and returns
only the final text. Like coding providers, they are never
served in the default read-only surface, and won't construct even with
`--allow-unsafe` unless the configured model's API key is set (default model
`deepseek-v4-flash`, override with `LAZYTOOLS_OPTIMIZER_AGENT_MODEL` /
`LAZYTOOLS_REPORT_AGENT_MODEL`).

### `code_review` — hand a review to Codex

`code_review` is the same idea pointed at *your own repositories*:
`lazybridge.Agent`s whose engine is
[`CodexEngine`](https://selvaz.github.io/LazyBridge/guides/full/codex-engine/)
— JSON-RPC to the locally authenticated `codex app-server`, inheriting its runtime authentication — served as three tools:

```text
codex_code_review(task, repo_path=None, diff_ref=None, paths=None, thread_id=None, session_name=None, model=None, effort=None) -> str
codex_ask(question, repo_path=None, thread_id=None, session_name=None, model=None, effort=None) -> str
codex_review_changes(repo_path=None, scope="uncommitted", ref=None, session_name=None, model=None, effort=None) -> str
```

`codex_code_review` finds defects in code you point it at; `codex_ask` answers a
design question about it ("does this protocol support X", "what breaks if I
change Y"); `codex_review_changes` runs Codex' *own* review harness over a typed
target (`uncommitted` / `branch` + ref / `commit` + ref) with no instructions
from us at all. The split is not cosmetic: the reviewer's instructions turn
every question into a findings list, and the native harness cannot be steered
because the protocol has no prompt slot for it.

Every Codex tool takes per-call `model` / `effort` overrides ("same question,
stronger model" is a legitimate consulting move; the env-var settings below
remain the defaults). `effort` is checked: one of `none`, `minimal`, `low`,
`medium`, `high`, `xhigh`, `max`, `ultra`, else a `ValueError` lists them. The consultant
differs from the reviewers in one more way: it carries the server's own **read-only LazyTools toolset** as Codex
dynamic tools — the `web`, `datahub`, `statistical`, `fin` and
`econ_calendar` providers, built from the same factories and configuration
the MCP server serves, so a consultant that searches the web or runs the
optimizer hits the same databases and caches as the equivalent MCP tools.
(Dynamic tools ride the app-server channel and need no MCP registration on
the participant's side — a Codex thread reaching this server through its own
`config.toml` MCP entry gets rejected by `approval_policy="never"` before
execution, which is exactly the path this avoids.) The reviewers get the
web too (since 2026-08-19 — a review can need to check a CVE or whether an
API is really deprecated), but not the LazyTools toolset: a reviewer reads
code, it does not run the optimizer.

Codex reads the files and runs `git` itself, so a call is "point it at a
repository and say what to look at":

```json
{"task": "is the new retry logic correct?", "repo_path": "LazyTools", "diff_ref": "main"}
```

**Follow-ups reuse native history.** Every reply's header carries `thread_id=<id>`; pass it
back and the next call continues the *same Codex thread*, which still holds what
it read and concluded, instead of re-exploring the repository from scratch:

```json
{"question": "and does that also affect the other caller?", "thread_id": "01a0...c509d3"}
```

Threads are durable (they live in the Codex CLI's own session store), so this
works across calls and across processes — but a thread belongs to the repository
it was opened on; don't reuse one against a different repo.

**Or name the conversation.** Pass `session_name="notes"` on any of these tools
and the thread is remembered under that alias (per repository, in LazyBridge's
`SessionRegistry` file): the first call opens it, later calls with the same name
resume it, and no id needs carrying. An explicit `thread_id` still wins and
re-points the name at it. The header then reads
`thread_id=<id> session_name=notes`.

It runs in Codex' **read-only sandbox** (`approval_policy="never"`, so nothing
can block the non-interactive transport): it reports, it never patches. Like
the other agent providers it is opt-in (`--allow-unsafe`) because a call spends
a real model turn; it is skipped entirely when the `codex` CLI can't be found.

| Setting | Default | Meaning |
|---|---|---|
| `code_root` (`--config`) / `LAZYTOOLS_CODE_ROOT` | server cwd | Directory every `repo_path` is confined to (and `paths` to `repo_path` in turn) — a caller cannot walk the reviewer out of it through an argument. The free-text `task` can still *ask*; the reviewer prompt refuses, but that is an instruction, not a sandbox, so keep secrets out of the root. |
| `LAZYTOOLS_CODE_REVIEW_MODEL` | the local `~/.codex/config.toml` model | Model override. |
| `LAZYTOOLS_CODE_REVIEW_EFFORT` | the CLI's default | Accepted vocabulary: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`; actual support depends on model/runtime. |
| `LAZYTOOLS_CODE_REVIEW_TIMEOUT` | `3600` | Seconds per review. |

A review takes minutes, so give the *host* a matching tool timeout (Claude
Code: `MCP_TOOL_TIMEOUT` in ms) — otherwise the host cancels the call before
Codex answers.

### `claude_review` — the same thing on the other model family

```text
claude_code_review(task, repo_path=None, diff_ref=None, paths=None, session_id=None, session_name=None, model=None, effort=None) -> str
claude_ask(question, repo_path=None, session_id=None, session_name=None, model=None, thinking=None, effort=None) -> str
```

`claude_ask` mirrors `codex_ask`'s consulting extras too: per-call `model` /
`thinking` / `effort` overrides (`effort`: `low`, `medium`, `high`, `xhigh`,
`max`), `session_name`, the same read-only LazyTools toolset (served to the
engine as its in-process MCP server), and the engine's own WebSearch/WebFetch
(`web=True`). `claude_code_review` gets that same web access — build it with
`claude_reviewer(web=False)` for the old offline behavior — but not the
LazyTools toolset.

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

Model via `LAZYTOOLS_CLAUDE_REVIEW_MODEL` (default `sonnet`), reasoning effort
via `LAZYTOOLS_CLAUDE_REVIEW_EFFORT`, extended thinking
via `LAZYTOOLS_CLAUDE_REVIEW_THINKING`; the confinement root and per-call
timeout are the same `LAZYTOOLS_CODE_ROOT` / `LAZYTOOLS_CODE_REVIEW_TIMEOUT` as
above. Registered as its own provider so a missing `codex` CLI cannot take the
Claude tools down with it, and vice versa.

### `code_write` — let Codex / Claude Code edit

```text
codex_write(task, cwd=None, thread_id=None, session_name=None, model=None, effort=None) -> dict | str
claude_code_write(task, cwd=None, session_id=None, session_name=None, model=None, effort=None) -> dict | str
```

Opt-in (`--allow-unsafe`), confined to the code root, and now run on the
LazyBridge engines like the review tools, with the same per-call `model`,
`effort` and `session_name`. **`codex_write` no longer takes `resume_last`** —
continue a conversation by `session_name` or the previous reply's `thread_id`.
Claude's writer edits only inside its `cwd` and runs `Bash` behind an
allow-list gate; Bash is not path-confined, so treat the code root as fully
trusted scope.

### `code_sessions` — see and manage session names

```text
code_sessions_list(kind=None, repo_path=None, limit=50, since_days=None) -> dict
code_sessions_bind(kind, name, native_id, repo_path=None) -> dict
code_sessions_rename(kind, old, new, repo_path=None) -> dict
code_sessions_forget(kind, name, repo_path=None) -> dict
```

`session_name` aliases live in LazyBridge's `SessionRegistry` (a JSON file,
`~/.lazybridge/sessions.json` or `LAZYBRIDGE_SESSIONS_FILE`), keyed by
`kind` (`codex` or `claude`), the resolved repository and the name.
`code_sessions_list` is read-only and returns `sessions`, `total` (matching rows
before the limit) and `returned` (included count). Each session has `name`, `kind`,
`native_id`, `scope`, `model`, `effort` and `updated_at`. Sessions are sorted by
`updated_at` descending. The default `limit` is 50, capped at 200 (minimum 1).
Optional non-negative `since_days` keeps sessions updated within that many days;
all filters apply before counting and limiting. The three mutators adopt a thread you
already have (`bind`), rename, or drop a name (`forget` never deletes the
underlying conversation); they are only served with `--allow-unsafe`, and
`repo_path` is confined to the code root like every other path.

## Safety model

Structured tool results use strict JSON: non-finite floats (`NaN`, positive or
negative infinity, including NumPy floats) and pandas NA become `null` recursively.
This also applies to model/dataclass conversions and JSON serializer output.
Plain string and byte results retain their text behavior.

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
from lazytools.mcp_server import build_server
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
