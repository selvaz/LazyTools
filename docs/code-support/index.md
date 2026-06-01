# Code Support Agent

Delegate coding work to **Claude Code** and **Codex** — the two coding CLIs —
from inside a LazyBridge agent, then let them collaborate.
`lazytools.connectors.code_support` gives each CLI two integration modes and a
collaboration pipeline that combines them.

| Agent | CLI mode | MCP mode | Guide |
|---|---|---|---|
| **Claude Code** | `claude_code` | `claude_code_mcp` | [Claude Code](claude-code.md) |
| **Codex** | `codex` | `codex_mcp` | [Codex](codex.md) |
| **Collaboration** | — | — | [Collaboration](collaboration.md) (`build_cli_collaboration`) |

!!! info "Status & install"
    **Status: alpha.** The CLI-mode tools (`claude_code`, `codex`,
    `build_cli_collaboration`) are stdlib-only (`subprocess`, `json`, `shutil`):
    ```bash
    pip install lazytoolkit
    ```
    The **MCP-mode** factories (`claude_code_mcp` / `codex_mcp`) additionally
    need the `mcp` extra:
    ```bash
    pip install 'lazytoolkit[mcp]'
    ```
    The package is `lazytoolkit` (PyPI); the import root is `lazytools`. The two
    CLIs are *not* Python packages — install them separately and make sure
    `claude` / `codex` are on `PATH`:

    - **Claude Code** — <https://docs.anthropic.com/claude-code>
    - **Codex** — <https://github.com/openai/codex>

## Two modes, one idea

Both agents run as **subprocesses** — not Python libraries, not HTTP APIs. What
differs is the *relationship* between your agent and the CLI:

| | **CLI mode** (`claude_code`, `codex`) | **MCP mode** (`claude_code_mcp`, `codex_mcp`) |
|---|---|---|
| Relationship | the CLI **is** the agent | the CLI exposes a tool surface; **your** agent orchestrates it |
| One call | a whole delegated task → result string | one MCP tool invocation |
| Built on | `subprocess.run` (stdlib) | the [MCP connector](../mcp.md) — needs `[mcp]` |
| Use when | you want to *delegate* a task | you want the CLI's primitives inside your own loop |

Pick CLI mode to hand off "do this task and tell me the result". Pick MCP mode
when you want your own agent to drive the CLI's individual tools (Claude's
`Read`/`Edit`/`Bash`, or Codex's `codex`/`codex-reply`) step by step.

## At a glance

=== "Claude Code (CLI)"

    ```python
    from lazybridge import Agent, LLMEngine
    from lazytools.connectors.code_support import claude_code

    agent = Agent(
        engine=LLMEngine("claude-opus-4-8", tool_timeout=None),
        tools=[claude_code],
    )
    print(agent("Analyse the auth module and flag any security issues").text())
    ```

=== "Codex (CLI)"

    ```python
    from lazybridge import Agent, LLMEngine
    from lazytools.connectors.code_support import codex

    agent = Agent(
        engine=LLMEngine("gpt-5.4", tool_timeout=None),
        tools=[codex],
    )
    print(agent("List the public functions in main.py and describe each").text())
    ```

=== "Claude Code (MCP)"

    ```python
    from lazybridge import Agent, LLMEngine
    from lazytools.connectors.code_support import claude_code_mcp

    # allow= is REQUIRED (deny-by-default). Patterns match the namespaced name.
    reader = claude_code_mcp(allow=["claude_code.Read", "claude_code.Glob"])
    agent = Agent(engine=LLMEngine("claude-opus-4-8"), tools=[reader])
    ```

=== "Codex (MCP)"

    ```python
    from lazybridge import Agent, LLMEngine
    from lazytools.connectors.code_support import codex_mcp

    codex_srv = codex_mcp(allow=["codex.codex", "codex.codex-reply"])
    agent = Agent(engine=LLMEngine("claude-opus-4-8"), tools=[codex_srv])
    ```

=== "Collaboration"

    ```python
    from lazytools.connectors.code_support import build_cli_collaboration

    # Claude Code analyses → Codex critiques → synthesizer plans → executor implements.
    pipeline = build_cli_collaboration()
    print(pipeline("Add rate limiting to the /api/login endpoint").text())
    ```

## Startup check

```python
from lazytools.connectors.code_support import check_clis_available

check_clis_available()      # {"claude": True, "codex": False}
```

`shutil.which` for both CLIs — call it at startup to fail fast on a missing CLI
instead of discovering it at the first tool call.

## Timeouts — set `tool_timeout=None`

`LLMEngine`'s `tool_timeout` wraps each tool call in `asyncio.wait_for`. But the
CLI-mode functions run `subprocess.run(...)` **in a thread pool**, and cancelling
the coroutine does *not* interrupt that thread — the subprocess keeps running
(orphaned) until *its own* `timeout` fires. So let the subprocess own the
deadline:

```python
LLMEngine("claude-opus-4-8", tool_timeout=None)   # subprocess timeout= is the only limit
```

If you do want an engine-level ceiling, set `tool_timeout` **strictly greater**
than the per-call `timeout` (e.g. `tool_timeout=320` with `timeout=300`).

## Security & safety

- **Read is the default.** CLI-mode tools default to `mode="read"` (Claude:
  `Read,Bash,Grep,Glob`; Codex: `-s read-only`). Opt into writes explicitly.
- **MCP mode is deny-by-default.** `claude_code_mcp` / `codex_mcp` require an
  `allow=` / `deny=` filter, exactly like `MCP.stdio` — and Claude Code's MCP
  server has **no per-call confirmation** of its own, so the allow-list is your
  primary control.
- **Secrets stay with the CLIs.** Auth is owned by each CLI's own login
  (`~/.claude/.credentials.json` / `ANTHROPIC_API_KEY` for Claude, `codex login`
  for Codex), inherited via the environment; the connector never reads, rewrites,
  or echoes tokens.
- **Errors are returned, not raised** (CLI mode). Missing CLI, non-zero exit, and
  timeouts come back as `"[claude_code] …"` / `"[codex] …"` strings so the model
  can recover instead of crashing the run.

## See also

- [Claude Code](claude-code.md) — CLI + MCP modes, full reference.
- [Codex](codex.md) — CLI + MCP modes, full reference.
- [Collaboration](collaboration.md) — the two agents working together.
- [Tools overview](../connectors.md) — every connector at a glance.
- [MCP](../mcp.md) — the connector the MCP mode is built on.
- [Safety](../safety.md) — gating dangerous tools.
