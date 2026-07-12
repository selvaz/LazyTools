# Codex

Put **Codex** behind a LazyBridge agent in either of two modes:

- **CLI mode** — [`codex`](#cli-mode-codex): hand the CLI a task via
  `codex exec`, it runs its own loop, you get the final message back.
- **MCP mode** — [`codex_mcp`](#mcp-mode-codex_mcp): run `codex mcp-server` and
  let your agent call Codex's two agent-level MCP tools (`codex` /
  `codex-reply`).

See [Code Support Agent](index.md) for install, the CLI-vs-MCP overview, the
startup check, and timeout guidance.

## CLI mode — `codex`

```python
from lazytools.connectors.code_support import codex

codex(
    task: str,
    *,
    cwd: str | None = None,
    resume_last: bool = False,
    timeout: float = 300.0,
    skip_git_check: bool = True,
) -> dict | str
```

Read-only by construction (`-s read-only`); on success returns
`{"result": <text>, "content_is_untrusted": true}`. There is deliberately
**no write mode here**: writes live behind
[`CodeWriteTools`](index.md#writes-codewritetools) (mandatory `base_dir`
sandbox + one-shot confirmation).

Wraps `codex exec "<task>"` and returns the final message printed on stdout.
Unlike Claude Code, Codex prints only the final message (not a JSON envelope),
and the session id is not on stdout — to continue, use `resume_last=True`
(`exec resume --last`).

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `task` | `str` | — | The instruction for Codex. |
| `mode` | `str` | `"read"` | `read` → `-s read-only` (safe default). `write` → `-s workspace-write --full-auto`. |
| `cwd` | `str \| None` | `None` | Working directory for the subprocess. |
| `resume_last` | `bool` | `False` | Continue the most recent session via `exec resume --last`. |
| `timeout` | `float` | `300.0` | Max seconds for the subprocess. |
| `skip_git_check` | `bool` | `True` | Pass `--skip-git-repo-check`; required outside a git repo. |

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.code_support import codex

agent = Agent(
    engine=LLMEngine("gpt-5.4", tool_timeout=None),
    tools=[codex],
)
print(agent("List the public functions in main.py and describe each").text())
```

!!! warning "`write` mode uses `--full-auto`, not `-a on-failure`"
    In a non-interactive subprocess, an approval prompt would **block waiting
    for stdin** on the first failure and hang until the timeout fires. `write`
    mode therefore uses `--full-auto`, which pairs `workspace-write` with a
    non-interactive approval policy. (`codex exec` has no `-a` approval flag.)
    Prefer a git repo for `write` so changes are reviewable.

**Auth.** Codex uses the credentials from `codex login` (`~/.codex/auth.json`);
the subprocess inherits the current shell environment. There is no
environment-variable seam as clean as Claude's, so run `codex login` first.

## MCP mode — `codex_mcp`

```python
from lazytools.connectors.code_support import codex_mcp

codex_mcp(
    *,
    name: str = "codex",
    allow: Iterable[str] | None = None,
    deny: Iterable[str] | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    namespace: bool = True,
    prefix: str | None = None,
    cache_tools_ttl: float | None = 60.0,
) -> MCPServer
```

Codex's MCP server is different in kind from Claude Code's: instead of file
primitives it exposes **two agent-level tools** (verified against Codex's
`codex_mcp_interface.md`):

| Tool (namespaced) | Purpose | Key arguments |
|---|---|---|
| `codex.codex` | Start a new Codex conversation and run it to completion | `prompt` (required); optional `model`, `cwd`, `sandbox`, `approval-policy`, `config` |
| `codex.codex-reply` | Continue an existing conversation | `prompt` + the `threadId` returned by a prior `codex` call |

So `codex_mcp` is closer to the `codex` *CLI tool* in spirit (you send a prompt,
Codex runs its own loop) — but delivered as MCP tools your agent can call and
chain via the returned `threadId`. The tool result is a standard MCP
`CallToolResult`; Codex mirrors the text plus the `threadId` inside
`structuredContent`, which the [MCP connector](../mcp.md) surfaces in the
flattened tool output so the follow-up `codex-reply` can chain.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | `"codex"` | Server name and default namespace prefix (`<name>.`). |
| `allow` | `Iterable[str] \| None` | `None` | fnmatch globs (against the **namespaced** name) to permit. **`allow=` or `deny=` is required.** |
| `deny` | `Iterable[str] \| None` | `None` | fnmatch globs to block. |
| `args` | `list[str] \| None` | `None` | Extra CLI args appended after `mcp-server`. |
| `env` | `dict[str, str] \| None` | `None` | Extra subprocess env (auth otherwise inherited). |
| `namespace` | `bool` | `True` | Prefix every tool with `<name>.`. `False` keeps raw names. |
| `prefix` | `str \| None` | `None` | Custom prefix instead of `<name>.`. |
| `cache_tools_ttl` | `float \| None` | `60.0` | Seconds the discovered tool list is cached; `None` = never expire. |

```python
from lazybridge import Agent, LLMEngine
from lazytools.connectors.code_support import codex_mcp

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

```python
srv = codex_mcp(allow=["*"])
agent = Agent(engine=LLMEngine("claude-opus-4-8"), tools=[srv])
print(sorted(agent._tool_map))   # ['codex.codex', 'codex.codex-reply', ...]
```

`allow=["*"]` connects the subprocess at construction time, so this doubles as a
smoke test that the CLI launches and authenticates.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `[codex] CLI 'codex' not found in PATH` | Codex not installed / not on `PATH` | Install it; verify with `check_clis_available()` |
| `[codex] error (exit 1): …` outside a repo | Codex's git-repo check | Keep `skip_git_check=True` (the default) |
| `[codex] timeout after 300s` | Task longer than `timeout` | Raise `timeout=`; set engine `tool_timeout=None` |
| Orphaned `codex` process after a run | Engine `tool_timeout` fired before the subprocess | Use `tool_timeout=None`, or `tool_timeout > timeout` |
| `ValueError: requires an explicit allow= / deny=` | `codex_mcp` called without a filter | Pass `allow=["*"]` (after auditing) or an explicit glob list |
| `ImportError: requires the official MCP SDK` | MCP mode used without the extra | `pip install "lazytoolkit[mcp] @ git+https://github.com/selvaz/LazyTools.git"` |

## Pitfalls

- **`resume --last` is ambiguous** when several sessions share a directory — it
  always takes the most recent. For parallel conversations, pass the full
  context in the prompt rather than relying on resume.
- **Codex MCP is experimental.** The `codex` / `codex-reply` tool shape can
  change between Codex versions — pin your version if you rely on it.

## See also

- [Claude Code](claude-code.md) — the other code-support agent.
- [Collaboration](collaboration.md) — Claude Code + Codex working together.
- [Code Support Agent](index.md) — install, modes, timeouts, startup check.
- [MCP](../mcp.md) — the connector MCP mode is built on.
