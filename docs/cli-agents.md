# CLI Agents

Delegate work to **Claude Code** and **Codex** — the two coding CLIs — from
inside a LazyBridge agent, then let them collaborate.
`lazytools.connectors.cli_agents` ships three drop-in tools:

| Tool | Kind | What it gives an agent |
|---|---|---|
| `claude_code` | function tool | Run a task through the Claude Code CLI and get the result back. |
| `codex` | function tool | Run a task through the Codex CLI and get the result back. |
| `build_cli_collaboration` | pipeline factory → Agent tool | Claude Code + Codex analysing, critiquing, planning and implementing together — as **one** tool. |
| `claude_code_mcp` / `codex_mcp` | MCP-server factory → tool provider | Expose each CLI's *own tools* over MCP, for your agent to orchestrate (the other integration shape). Needs the `mcp` extra. |

!!! info "Status & install"
    **Status: alpha.** The function tools (`claude_code`, `codex`,
    `build_cli_collaboration`) are stdlib-only (`subprocess`, `json`, `shutil`):
    ```bash
    pip install lazytoolkit
    ```
    The **MCP-server variants** (`claude_code_mcp` / `codex_mcp`) additionally
    need the `mcp` extra:
    ```bash
    pip install 'lazytoolkit[mcp]'
    ```
    The package is `lazytoolkit` (PyPI); the import root is `lazytools`. The two
    CLIs are *not* Python packages — install them separately and make sure
    `claude` / `codex` are on `PATH`:

    - **Claude Code** — <https://docs.anthropic.com/claude-code>
    - **Codex** — <https://github.com/openai/codex>

## Synopsis

Claude Code and Codex are **CLIs that run as subprocesses** — not Python
libraries, not HTTP APIs. You hand them a prompt on the command line; they read
files, write code, run commands, and print a textual result. Each connector is a
plain function that builds the right command line, runs `subprocess.run(...)`
with `env` / `cwd` / `timeout`, parses the output, and returns a clean string
(or a tidy error string — it never raises into your agent loop).

Because they are ordinary sync callables, `Tool.run` dispatches them to a thread
pool automatically, so a slow CLI never blocks the event loop.

```
claude_code(task, mode="read")     codex(task, mode="read")
───────────────────────────────   ─────────────────────────
claude -p "…" --output-format json   codex exec "…" -s read-only
  │ parse JSON .result                  │ stdout = final message
  ▼                                     ▼
clean string                          clean string
```

## The key asymmetry

The two CLIs differ in how they report results, which is why the two functions
are not identical under the hood:

- **Claude Code** with `--output-format json` returns one clean JSON object —
  `result`, `session_id`, `total_cost_usd`, `subtype` (`success` | `error`).
  Easy to parse; carries the session id for `--resume`.
- **Codex** with `exec` prints **only** the final message on stdout. The session
  id is *not* on stdout — to continue you use `exec resume --last`. So
  `claude_code` exposes `session_id=`, while `codex` exposes `resume_last=`.

## `claude_code`

```python
from lazytools.connectors.cli_agents import claude_code

claude_code(
    task: str,
    *,
    mode: str = "read",          # "read" | "write" | "plan"
    cwd: str | None = None,
    session_id: str | None = None,
    timeout: float = 300.0,
) -> str
```

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `task` | `str` | — | The instruction for Claude Code. |
| `mode` | `str` | `"read"` | `read` → `Read,Bash,Grep,Glob` (analysis only, safe default). `write` → adds `Write,Edit` with `--permission-mode acceptEdits`. `plan` → `--permission-mode plan`, no edits. |
| `cwd` | `str \| None` | `None` | Working directory for the subprocess. |
| `session_id` | `str \| None` | `None` | If set, resumes an existing session via `--resume`. |
| `timeout` | `float` | `300.0` | Max seconds for the subprocess. |

**Auth.** Left to the CLI. `claude_code` passes no custom environment, so the
Claude Code CLI uses its own on-disk login (`~/.claude/.credentials.json`), and
the inherited environment still carries `CLAUDE_CODE_OAUTH_TOKEN` (the token
*string* from `claude setup-token`) or `ANTHROPIC_API_KEY` if you set them. The
tool deliberately does **not** synthesize `CLAUDE_CODE_OAUTH_TOKEN` from the
JSON credential store — that env var is a token string, not the store, and
overriding it would break a valid disk login.

## `codex`

```python
from lazytools.connectors.cli_agents import codex

codex(
    task: str,
    *,
    mode: str = "read",          # "read" | "write"
    cwd: str | None = None,
    resume_last: bool = False,
    timeout: float = 300.0,
    skip_git_check: bool = True,
) -> str
```

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `task` | `str` | — | The instruction for Codex. |
| `mode` | `str` | `"read"` | `read` → `-s read-only` (safe default). `write` → `-s workspace-write --full-auto`. |
| `cwd` | `str \| None` | `None` | Working directory for the subprocess. |
| `resume_last` | `bool` | `False` | Continue the most recent session via `exec resume --last`. |
| `timeout` | `float` | `300.0` | Max seconds for the subprocess. |
| `skip_git_check` | `bool` | `True` | Pass `--skip-git-repo-check`; required outside a git repo. |

!!! warning "`write` mode uses `--full-auto`, not `-a on-failure`"
    In a non-interactive subprocess, `-a on-failure` would **block waiting for
    stdin** on the first failure and hang until the timeout fires. `write` mode
    therefore uses `--full-auto` so the run completes without prompts. Prefer a
    git repo for `write` so changes are reviewable.

**Auth.** Codex uses the credentials from `codex login` (`~/.codex/auth.json`);
the subprocess inherits the current shell environment. There is no
environment-variable seam as clean as Claude's, so run `codex login` first.

## `build_cli_collaboration`

The collaboration pipeline — *Claude Code analyses, Codex critiques, a
synthesizer plans, an executor implements* — packaged as a **single tool**, the
same way you'd pass `claude_code` or `codex`:

```python
from lazytools.connectors.cli_agents import build_cli_collaboration

build_cli_collaboration(
    *,
    name: str = "cli_collaboration",
    description: str | None = None,
    claude_model: str = "claude-opus-4-8",
    codex_model: str = "gpt-5.4",
    synthesizer_model: str = "claude-opus-4-8",
    executor_model: str = "claude-opus-4-8",
    execute: bool = True,
) -> Agent
```

It returns a named `Agent` whose engine is a four-step `Plan`. Because an
`Agent` *is* a tool in LazyBridge, the whole pipeline looks to the parent agent
like one callable that takes a single `task` string.

```
Step 1  claude_analyst   claude_code(read)   analyse, propose an approach
Step 2  codex_analyst    codex(read)         critique/confirm  (sees step 1 via shared Memory)
Step 3  synthesizer      —                   merge into one concrete plan
Step 4  executor         claude_code(write)  implement the plan        (skipped when execute=False)
```

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | `"cli_collaboration"` | Tool name the parent agent sees (tool-map key). |
| `description` | `str \| None` | `None` | Tool description shown to the parent LLM; a sensible default is used when `None`. |
| `claude_model` | `str` | `"claude-opus-4-8"` | Model for the Claude-Code analyst (step 1). |
| `codex_model` | `str` | `"gpt-5.4"` | Model for the Codex analyst/critic (step 2). |
| `synthesizer_model` | `str` | `"claude-opus-4-8"` | Model that merges the analyses (step 3). |
| `executor_model` | `str` | `"claude-opus-4-8"` | Model that implements the plan (step 4). |
| `execute` | `bool` | `True` | `True` → implement (writes files). `False` → stop after synthesis (read-only "analyse + plan"). |

**Why `Plan`, not `AgentPool`?** The flow is fixed and sequential
(analyse → critique → synthesise → execute). `Plan` with `from_step` is simpler
and more predictable, and each step frees memory before the next. Reach for
`AgentPool` only when you need dynamic routing or a multi-round back-and-forth.

## Startup check

```python
from lazytools.connectors.cli_agents import check_clis_available

check_clis_available()      # {"claude": True, "codex": False}
```

`shutil.which` for both CLIs — call it at startup to fail fast on a missing CLI
instead of discovering it at the first tool call.

## Timeouts — set `tool_timeout=None`

`LLMEngine`'s `tool_timeout` wraps each tool call in `asyncio.wait_for`. But the
functions run `subprocess.run(...)` **in a thread pool**, and cancelling the
coroutine does *not* interrupt that thread — the subprocess keeps running
(orphaned) until *its own* `timeout` fires. So let the subprocess own the
deadline:

```python
LLMEngine("claude-opus-4-8", tool_timeout=None)   # subprocess timeout= is the only limit
```

If you do want an engine-level ceiling, set `tool_timeout` **strictly greater**
than the per-call `timeout` (e.g. `tool_timeout=320` with `timeout=300`).

## Examples

=== "Phase 1 — Claude Code"

    ```python
    from lazybridge import Agent, LLMEngine
    from lazytools.connectors.cli_agents import claude_code

    agent = Agent(
        engine=LLMEngine("claude-opus-4-8", tool_timeout=None),
        tools=[claude_code],
    )
    print(agent("Analyse the auth module and flag any security issues").text())
    ```

=== "Phase 2 — Codex"

    ```python
    from lazybridge import Agent, LLMEngine
    from lazytools.connectors.cli_agents import codex

    agent = Agent(
        engine=LLMEngine("gpt-5.4", tool_timeout=None),
        tools=[codex],
    )
    print(agent("List the public functions in main.py and describe each").text())
    ```

=== "Phase 3 — collaboration"

    ```python
    from lazytools.connectors.cli_agents import build_cli_collaboration

    # The whole Claude Code + Codex pipeline as a single tool.
    pipeline = build_cli_collaboration()
    print(pipeline("Add rate limiting to the /api/login endpoint").text())
    ```

=== "Phase 3 — as a sub-tool"

    ```python
    from lazybridge import Agent, LLMEngine
    from lazytools.connectors.cli_agents import build_cli_collaboration

    # Hand the collaboration to a higher-level orchestrator, alongside any
    # other tools — it behaves like any other tool that takes a task string.
    orchestrator = Agent(
        engine=LLMEngine("claude-opus-4-8"),
        tools=[build_cli_collaboration(name="deep_code_task")],
    )
    orchestrator("Use deep_code_task to add retries to the HTTP client")
    ```

=== "Read-only (analyse + plan)"

    ```python
    from lazytools.connectors.cli_agents import build_cli_collaboration

    # execute=False stops after synthesis — never writes files.
    planner = build_cli_collaboration(execute=False)
    print(planner("Propose a refactor of the payments module").text())
    ```

## MCP-server variant — `claude_code_mcp` / `codex_mcp`

Both CLIs can also run as **MCP servers**. This is a fundamentally *different
relationship* from the function tools above — not a different transport for the
same thing:

| | `claude_code` / `codex` (function tools) | `claude_code_mcp` / `codex_mcp` (MCP servers) |
|---|---|---|
| **Relationship** | the CLI **is** the agent | the CLI exposes a tool surface; **your** agent orchestrates it |
| **One call** | a whole delegated task → final result | one MCP tool invocation |
| **Returns** | result string (Claude: + `session_id`, cost) | per-tool MCP `CallToolResult` (flattened to text) |
| **Built on** | `subprocess.run` (stdlib) | the [MCP connector](mcp.md) — needs `pip install 'lazytoolkit[mcp]'` |
| **Confirmation** | the CLI's own permission mode | **your client owns confirmation** — see safety note |

Each factory is a thin wrapper over [`MCP.stdio`](mcp.md) with the verified
launch command (`claude mcp serve` / `codex mcp-server`). Everything `MCP.stdio`
does applies unchanged: **lazy connect** on first use, **namespacing**
(`claude_code.<tool>` / `codex.<tool>`), **deny-by-default** `allow=`/`deny=`
filtering, and the **TTL tool-discovery cache**.

### Factory settings

Both factories take the same keyword-only parameters:

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | `"claude_code"` / `"codex"` | Server name and default namespace prefix (`<name>.`). |
| `allow` | `Iterable[str] \| None` | `None` | fnmatch globs (against the **namespaced** name) to permit. **`allow=` or `deny=` is required.** |
| `deny` | `Iterable[str] \| None` | `None` | fnmatch globs to block. For Claude this is the natural way to drop `Bash`/`Write` while keeping the rest. |
| `args` | `list[str] \| None` | `None` | Extra CLI args appended after `mcp serve` / `mcp-server`. |
| `env` | `dict[str, str] \| None` | `None` | Extra environment for the subprocess. Auth is otherwise inherited (Claude on-disk login / `codex login`). |
| `namespace` | `bool` | `True` | Prefix every tool with `<name>.`. `False` keeps raw names. |
| `prefix` | `str \| None` | `None` | Custom prefix instead of `<name>.`. |
| `cache_tools_ttl` | `float \| None` | `60.0` | Seconds the discovered tool list is cached; `None` = never expire. |

`allow` / `deny` are **required** (deny-by-default) — the same posture as
`MCP.stdio`, because these servers expose file-write and shell tools. Pass
`allow=["*"]` only after you've audited the surface.

---

### `claude_code_mcp` — Claude Code's tools over MCP

`claude mcp serve` exposes **Claude Code's own built-in tools** to your agent:
`Read`/`View`, `Edit`, `Write`, `LS`, `Glob`, `Grep`, `Bash`, and others
(`NotebookEdit`, `WebFetch`, `WebSearch`, `TodoWrite`, …). Your agent calls them
like any other tool; there is *no* Claude model in the loop — these are the raw
file/shell primitives.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.cli_agents import claude_code_mcp

# Read-only slice: analysis tools only, no Edit/Write/Bash.
reader = claude_code_mcp(allow=["claude_code.Read", "claude_code.LS",
                                "claude_code.Glob", "claude_code.Grep"])

agent = Agent(engine=LLMEngine("claude-opus-4-8"), tools=[reader])
agent("Find every TODO in src/ and summarise them by file")
```

The agent now sees tools named `claude_code.Read`, `claude_code.Glob`, … and
invokes them directly. A read-then-edit setup just widens the allow-list:

```python
editor = claude_code_mcp(allow=["claude_code.Read", "claude_code.Edit",
                                "claude_code.Glob", "claude_code.Grep"])
# Or: keep everything except the dangerous shell, via deny=
safe = claude_code_mcp(deny=["claude_code.Bash"])
```

!!! danger "Your client owns confirmation"
    Claude's docs are explicit: when Claude Code runs as an MCP server it only
    *exposes its tools* — **your MCP client is responsible for confirming
    individual tool calls.** There is no built-in `acceptEdits`/`plan` gate here
    (unlike the [`claude_code`](#claude_code) function tool). Treat `Edit`,
    `Write`, and especially `Bash` as live, unconfirmed capabilities and gate
    them with an `allow=`/`deny=` list (and, if needed, LazyBridge
    [guards](safety.md) / human-in-the-loop).

### `codex_mcp` — Codex as an agent, over MCP

Codex's MCP server is different in kind: instead of file primitives it exposes
**two agent-level tools** (verified against Codex's `codex_mcp_interface.md`):

| Tool (namespaced) | Purpose | Key arguments |
|---|---|---|
| `codex.codex` | Start a new Codex conversation and run it to completion | `prompt` (required); optional `model`, `cwd`, `sandbox`, `approval-policy`, `config` |
| `codex.codex-reply` | Continue an existing conversation | `prompt` + the `threadId` returned by a prior `codex` call |

So `codex_mcp` is closer to the `claude_code` *function tool* in spirit (you
send a prompt, Codex runs its own loop) — but delivered as MCP tools your agent
can call and chain via the returned `threadId`. The tool result is a standard
MCP `CallToolResult`; Codex also mirrors the text plus the `threadId` inside
`structuredContent`.

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.cli_agents import codex_mcp

codex_srv = codex_mcp(allow=["codex.codex", "codex.codex-reply"])

agent = Agent(engine=LLMEngine("claude-opus-4-8"), tools=[codex_srv])
# The agent calls codex.codex(prompt=...), gets a threadId back in the result,
# then calls codex.codex-reply(prompt=..., threadId=...) to continue.
agent("Use codex to add a retry decorator to http.py, then ask it to add a test")
```

!!! warning "Codex MCP is experimental"
    OpenAI documents the `codex mcp-server` interface as **experimental and
    subject to change without notice**. The `codex` / `codex-reply` tool names
    and their argument shapes can move between Codex versions — pin your Codex
    version if you depend on them, and re-check with `allow=["*"]` after an
    upgrade.

### Discovering the live tool surface

Tool names and schemas belong to the *installed* CLI version, so the connector
never hardcodes them. To see exactly what a server advertises, allow everything
once and inspect the agent's tool map:

```python
srv = claude_code_mcp(allow=["*"])           # or codex_mcp(allow=["*"])
agent = Agent(engine=LLMEngine("claude-opus-4-8"), tools=[srv])
print(sorted(agent._tool_map))               # ['claude_code.Bash', 'claude_code.Edit', ...]
```

Then tighten `allow=` to just the tools you want. (`allow=["*"]` connects the
subprocess at construction time, so this also doubles as a smoke test that the
CLI launches and authenticates.)

### Tuning the discovery cache

The discovered tool list is cached for `cache_tools_ttl` seconds (default 60).
If a CLI upgrade changes the surface mid-process, force a refresh:

```python
srv = codex_mcp(allow=["*"], cache_tools_ttl=None)   # never auto-expire
# ... later, after upgrading the CLI:
srv.invalidate_tools_cache()                          # next call re-discovers
```

### Lifecycle

Like any `MCPServer`, the subprocess connects lazily on first use and stays up
for the process lifetime. For deterministic cleanup use it as an async context
manager:

```python
async with codex_mcp(allow=["codex.codex"]) as srv:
    agent = Agent(engine=LLMEngine("claude-opus-4-8"), tools=[srv])
    await agent.run("…")
# subprocess closed here; the server is single-shot afterwards
```

## When to use it

- **You already drive Claude Code / Codex by hand** and want an agent to delegate
  to them programmatically.
- **You want a second opinion**: `build_cli_collaboration` makes the two models
  critique each other before any code is written.
- **A larger agent needs a "do this coding task" capability** — drop the pipeline
  in as one tool and let the orchestrator call it.
- **You want the CLI's primitives, not its agent loop** — use the MCP variant so
  *your* agent orchestrates `Read`/`Edit`/`Bash` (Claude) or `codex` /
  `codex-reply` (Codex) directly.

## When NOT to use it

- **The CLIs aren't installed / authenticated.** These tools shell out; without
  `claude` / `codex` on `PATH` (and logged in) every call returns an error
  string. Run `check_clis_available()` first.
- **You need structured, programmatic file edits** with no model in the loop —
  write a plain `Tool` against your own code instead.
- **Hard latency budgets.** A CLI subprocess that itself runs an agent loop can
  take minutes; size your `timeout` accordingly and prefer `tool_timeout=None`.

## Security & safety

- **Read is the default.** Both functions default to `mode="read"` (Claude:
  `Read,Bash,Grep,Glob`; Codex: `-s read-only`). Opt into writes explicitly.
- **Writes are real.** `mode="write"` lets the CLI modify files in `cwd`. Point
  it at a repo you can review/roll back; Codex `write` runs `--full-auto`.
- **Secrets stay with the CLIs.** Auth is owned by each CLI's own login
  (`~/.claude/.credentials.json` / `ANTHROPIC_API_KEY` for Claude, `codex login`
  for Codex), inherited via the environment; the connector never reads, rewrites,
  or echoes tokens into tool results.
- **Errors are returned, not raised.** Missing CLI, non-zero exit, and timeouts
  come back as `"[claude_code] …"` / `"[codex] …"` strings (full stderr is logged
  via `logging`, truncated to 500 chars in the returned string) so the model can
  recover instead of crashing the run.
- **MCP variant inherits the MCP guards.** `claude_code_mcp` / `codex_mcp` are
  deny-by-default: you must pass `allow=` / `deny=`, exactly like `MCP.stdio`.
  But note Claude Code's MCP server has **no per-call confirmation** of its own
  — the allow-list is your primary control.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `[claude_code] CLI 'claude' not found in PATH` | Claude Code not installed / not on `PATH` | Install it; verify with `check_clis_available()` |
| `[codex] CLI 'codex' not found in PATH` | Codex not installed / not on `PATH` | Install it; verify with `check_clis_available()` |
| `[claude_code] error (exit 1): … auth …` | No valid Claude credentials | Sign in to Claude Code, or set `ANTHROPIC_API_KEY` |
| `[codex] error (exit 1): …` outside a repo | Codex's git-repo check | Keep `skip_git_check=True` (the default) |
| `[claude_code] timeout after 300s` / `[codex] timeout …` | Task longer than `timeout` | Raise `timeout=`; set engine `tool_timeout=None` |
| Orphaned CLI process after a run | Engine `tool_timeout` fired before the subprocess | Use `tool_timeout=None`, or `tool_timeout > timeout` |
| `ValueError: requires an explicit allow= / deny=` | `claude_code_mcp` / `codex_mcp` called without a filter | Pass `allow=["*"]` (after auditing) or an explicit glob list |
| `ImportError: requires the official MCP SDK` | MCP variant used without the extra | `pip install 'lazytoolkit[mcp]'` |

## Pitfalls

- **`tool_timeout` vs subprocess `timeout`.** The engine can't kill a thread-pool
  subprocess; let the subprocess own the deadline (`tool_timeout=None`).
- **Codex `resume --last` is ambiguous** when several sessions share a directory
  — it always takes the most recent. For parallel conversations, pass the full
  context in the prompt rather than relying on resume.
- **`build_cli_collaboration` writes by default** (`execute=True`). Pass
  `execute=False` for an analyse-and-plan-only run.
- **Shared `Memory` is sequential-only.** The pipeline's `dialogue` memory is
  safe because `Plan` steps don't overlap; don't reuse the pattern under
  parallel execution without a per-agent memory.
- **MCP variant: your client owns confirmation.** `claude_code_mcp` exposes
  `Edit`/`Write`/`Bash` with no built-in approval gate — restrict via
  `allow=`/`deny=` and add a guard if the agent is untrusted.
- **Codex MCP is experimental.** The `codex` / `codex-reply` tool shape can
  change between Codex versions — pin your version if you rely on it.

## See also

- [Tools overview](connectors.md) — every connector at a glance.
- [MCP](mcp.md) — the connector the MCP variant is built on; bring any MCP
  server's tools into an agent.
- [Safety](safety.md) — gating dangerous tools with `Allowlist` / `ConfirmationGate`.
