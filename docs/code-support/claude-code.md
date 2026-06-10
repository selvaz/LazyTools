# Claude Code

Put **Claude Code** behind a LazyBridge agent in either of two modes:

- **CLI mode** — [`claude_code`](#cli-mode-claude_code): hand the CLI a task, it
  runs its own loop, you get a result string.
- **MCP mode** — [`claude_code_mcp`](#mcp-mode-claude_code_mcp): run
  `claude mcp serve` and let *your* agent orchestrate Claude Code's own tools
  (View, Edit, LS, Bash, …).

See [Code Support Agent](index.md) for install, the CLI-vs-MCP overview, the
startup check, and timeout guidance.

## CLI mode — `claude_code`

```python
from lazytools.connectors.code_support import claude_code

claude_code(
    task: str,
    *,
    mode: str = "read",          # "read" | "write" | "plan"
    cwd: str | None = None,
    session_id: str | None = None,
    timeout: float = 300.0,
) -> str
```

Wraps `claude -p "<task>" --output-format json` and returns the parsed
`result`. The JSON envelope also carries `session_id`, `total_cost_usd`, and a
`subtype` (`success` | `error`); an error subtype is surfaced as the returned
string rather than raised.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `task` | `str` | — | The instruction for Claude Code. |
| `mode` | `str` | `"read"` | `read` → `Read,Grep,Glob` (analysis only — no Bash, so the CLI cannot run commands or modify files; safe default). `write` → `Read,Write,Edit,Bash,Grep,Glob` with `--permission-mode acceptEdits`. `plan` → `--permission-mode plan`, no edits. |
| `cwd` | `str \| None` | `None` | Working directory for the subprocess. |
| `session_id` | `str \| None` | `None` | If set, resumes an existing session via `--resume`. |
| `timeout` | `float` | `300.0` | Max seconds for the subprocess. |

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.code_support import claude_code

agent = Agent(
    engine=LLMEngine("claude-opus-4-8", tool_timeout=None),
    tools=[claude_code],
)
print(agent("Analyse the auth module and flag any security issues").text())
```

**Auth.** Left to the CLI. `claude_code` passes no custom environment, so the
Claude Code CLI uses its own on-disk login (`~/.claude/.credentials.json`), and
the inherited environment still carries `CLAUDE_CODE_OAUTH_TOKEN` (the token
*string* from `claude setup-token`) or `ANTHROPIC_API_KEY` if you set them. The
tool deliberately does **not** synthesize `CLAUDE_CODE_OAUTH_TOKEN` from the
JSON credential store — that env var is a token string, not the store, and
overriding it would break a valid disk login.

## MCP mode — `claude_code_mcp`

```python
from lazytools.connectors.code_support import claude_code_mcp

claude_code_mcp(
    *,
    name: str = "claude_code",
    allow: Iterable[str] | None = None,
    deny: Iterable[str] | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    namespace: bool = True,
    prefix: str | None = None,
    cache_tools_ttl: float | None = 60.0,
) -> MCPServer
```

`claude mcp serve` exposes **Claude Code's own built-in tools** to your agent:
`Read`/`View`, `Edit`, `Write`, `LS`, `Glob`, `Grep`, `Bash`, and others
(`NotebookEdit`, `WebFetch`, `WebSearch`, `TodoWrite`, …). Your agent calls them
like any other tool; there is *no* Claude model in the loop — these are the raw
file/shell primitives. The factory is a thin wrapper over
[`MCP.stdio`](../mcp.md), so namespacing, lazy connect, deny-by-default
filtering, and the discovery cache all apply.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | `"claude_code"` | Server name and default namespace prefix (`<name>.`). |
| `allow` | `Iterable[str] \| None` | `None` | fnmatch globs (against the **namespaced** name) to permit. **`allow=` or `deny=` is required.** |
| `deny` | `Iterable[str] \| None` | `None` | fnmatch globs to block — the natural way to drop `Bash`/`Write` while keeping the rest. |
| `args` | `list[str] \| None` | `None` | Extra CLI args appended after `mcp serve`. |
| `env` | `dict[str, str] \| None` | `None` | Extra subprocess env (auth otherwise inherited). |
| `namespace` | `bool` | `True` | Prefix every tool with `<name>.`. `False` keeps raw names. |
| `prefix` | `str \| None` | `None` | Custom prefix instead of `<name>.`. |
| `cache_tools_ttl` | `float \| None` | `60.0` | Seconds the discovered tool list is cached; `None` = never expire. |

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.code_support import claude_code_mcp

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
    (unlike the [`claude_code`](#cli-mode-claude_code) CLI tool). Treat `Edit`,
    `Write`, and especially `Bash` as live, unconfirmed capabilities and gate
    them with an `allow=`/`deny=` list (and, if needed, LazyBridge
    [guards](../safety.md) / human-in-the-loop).

### Discovering the live tool surface

Tool names belong to the *installed* Claude Code version, so the factory never
hardcodes them. To see what's advertised, allow everything once and inspect the
agent's tool map, then tighten:

```python
srv = claude_code_mcp(allow=["*"])
agent = Agent(engine=LLMEngine("claude-opus-4-8"), tools=[srv])
print(sorted(agent._tool_map))   # ['claude_code.Bash', 'claude_code.Edit', ...]
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `[claude_code] CLI 'claude' not found in PATH` | Claude Code not installed / not on `PATH` | Install it; verify with `check_clis_available()` |
| `[claude_code] error (exit 1): … auth …` | No valid Claude credentials | Sign in to Claude Code, or set `ANTHROPIC_API_KEY` |
| `[claude_code] timeout after 300s` | Task longer than `timeout` | Raise `timeout=`; set engine `tool_timeout=None` |
| Orphaned `claude` process after a run | Engine `tool_timeout` fired before the subprocess | Use `tool_timeout=None`, or `tool_timeout > timeout` |
| `ValueError: requires an explicit allow= / deny=` | `claude_code_mcp` called without a filter | Pass `allow=["*"]` (after auditing) or an explicit glob list |
| `ImportError: requires the official MCP SDK` | MCP mode used without the extra | `pip install 'lazytoolkit[mcp]'` |

## See also

- [Codex](codex.md) — the other code-support agent.
- [Collaboration](collaboration.md) — Claude Code + Codex working together.
- [Code Support Agent](index.md) — install, modes, timeouts, startup check.
- [MCP](../mcp.md) — the connector MCP mode is built on.
