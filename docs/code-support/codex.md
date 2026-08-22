# Codex

Put **Codex** behind a LazyBridge agent in one of three modes:

- **CLI mode** — [`codex`](#cli-mode-codex): hand the CLI a task via
  `codex exec`, it runs its own loop, you get the final message back.
- **MCP mode** — [`codex_mcp`](#mcp-mode-codex_mcp): run `codex mcp-server` and
  let your agent call Codex's two agent-level MCP tools (`codex` /
  `codex-reply`).
- **Review mode** — [`codex_reviewer`](#review-mode-codex_reviewer): Codex as
  the *engine* of a LazyBridge agent (`CodexEngine` → `codex app-server`),
  pinned to a reviewer prompt and exposed as one `codex_code_review` tool —
  plus [`codex_review_changes`](#codex_review_changes-codex-own-review-harness)
  (Codex' native `review/start` harness) and
  [`codex_ask`](#codex_ask-the-same-thing-for-questions) (design questions).

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

!!! warning "`write` mode pins `-c approval_policy=never`, not `--full-auto`"
    In a non-interactive subprocess, an approval prompt would **block waiting
    for stdin** on the first failure and hang until the timeout fires.
    Current `codex exec` builds have no `--full-auto`/`-a` flag at all
    (passing either is a hard argument-parsing error) — only `-s/--sandbox`
    and generic `-c key=value` config overrides. `write` mode therefore pairs
    `-s workspace-write` with `-c approval_policy=never`, pinning the same
    non-interactive guarantee `--full-auto` used to provide on older builds.
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

## Review mode — `codex_reviewer`

```python
from lazytools.connectors.code_support import codex_reviewer

tool = codex_reviewer(
    root: str | None = None,        # confines every call's repo_path
    model: str | None = None,       # None → whatever ~/.codex/config.toml says
    effort: str | None = None,      # "low" / "medium" / "high"
    timeout: float = 900.0,         # seconds per review
    name: str = "codex_code_review",
    system: str = CODE_REVIEWER_SYSTEM,
) -> Tool
```

Neither of the modes above is quite a *review*: CLI mode is a one-shot
`codex exec` with no fixed role, MCP mode hands you primitives. `codex_reviewer`
builds the thing itself — a `lazybridge.Agent` whose engine is
[`CodexEngine`](https://selvaz.github.io/LazyBridge/guides/full/codex-engine/)
(JSON-RPC to `codex app-server`, reusing the CLI's login — no API key), with
`CODE_REVIEWER_SYSTEM` as developer instructions — and returns it as a single
tool:

```python
await tool.run(
    task="is the new retry logic correct?",
    repo_path="LazyTools",   # absolute, or relative to root
    diff_ref="main",         # optional: scope to `git diff main...HEAD` + uncommitted
    paths="src/a.py",        # optional: restrict further
)
```

Codex reads the files and runs `git` itself, which is why this takes a
`repo_path` per call instead of being `Tool.wrap(agent)` (an agent-as-tool takes
only `task: str`, and the engine's `cwd` is fixed at construction).

- **Read-only.** The engine keeps `CodexPolicy`'s defaults —
  `sandbox="read-only"`, `approval_policy="never"` — so the reviewer reports
  and never patches, and no approval prompt can block a non-interactive
  transport. Writes stay behind [`CodeWriteTools`](index.md#writes-codewritetools).
- **Confined.** `repo_path` must resolve inside `root` (default:
  `LAZYTOOLS_CODE_ROOT` or the process cwd), and every entry in `paths` must
  resolve inside `repo_path`; anything else raises `ValueError`. Both checks
  matter: read-only stops *writes*, not reads elsewhere on the host, so an
  absolute path in `paths` would otherwise be quoted into the prompt as a file
  to go read. The free-text `task` is the one thing no check can constrain —
  `CODE_REVIEWER_SYSTEM` tells the reviewer to refuse reads outside the
  working directory, which is an instruction, not a sandbox. Don't point this
  tool at a root containing secrets you would not show the model.
- **Errors come back as text** (`[codex_code_review] failed in <cwd>: …`) rather
  than raising, so an orchestrating agent sees the failure instead of losing the
  turn.

### Continuing a review — `thread_id`

Each call runs on a **durable Codex thread** and reports its id in the header
(`[codex_code_review] <cwd> thread_id=<id>`). Pass that id back and the next
call resumes the same conversation: Codex still has the files it read and the
conclusions it drew, so a follow-up costs a turn, not a re-exploration.

```python
first = await tool.run(task="review the retry path", repo_path="LazyBridge")
tid = first.split("thread_id=")[1].split()[0]   # e.g. "LazyBridge#01a0...d3"
await tool.run(task="what about the timeout interaction?", repo_path="LazyBridge", thread_id=tid)
```

The handle is `<repo>#<id>`, not a bare id, and resuming it against a different
repository is refused: path confinement says nothing about a thread, so a
mis-pasted id would otherwise splice another repository's transcript into the
answer. Naming the repo in the handle keeps that check working across an MCP
server restart, with no stored state.

The engine stops sending LazyBridge `Memory` on a resumed thread — Codex owns
the history there, and two chronologies of one conversation is worse than none.
Threads live in the Codex CLI's own session store, so they also show up in its
history.

### `codex_review_changes` — Codex' own review harness

```python
from lazytools.connectors.code_support import codex_native_reviewer

tool = codex_native_reviewer(root=...)      # -> Tool named "codex_review_changes"
await tool.run(repo_path="LazyBridge", scope="branch", ref="main")
```

This one does **not** send a prompt. It calls the App Server's `review/start`
with a typed target — `uncommitted`, `branch` + `ref`, or `commit` + `ref` — and
returns what Codex' built-in review harness produces: findings tagged by
severity with file:line.

| | `codex_code_review` | `codex_review_changes` |
|---|---|---|
| Instruction | your `task`, steerable | none — the target *is* the instruction |
| Standards | `CODE_REVIEWER_SYSTEM` | Codex' own review harness |
| Use for | "is the retry logic correct?" | "review this branch, your call" |

Measured against codex-cli 0.148.0 on a repo with one planted defect: all three
targets find it and rank it (`[P1]`/`[P2]`); `uncommittedChanges` correctly
judged a benign edit benign. Findings arrive as one text message, not a
structured array — there is no `findings[]` API to parse. Only `delivery:
"inline"` is wired: a detached review completes on a *different* thread and
raises an approval request the parent thread never sees, which cannot work for
a non-interactive caller.

Because the review runs inline on a durable thread, the `thread_id` it returns
can go straight to `codex_ask` — that is how you interrogate the findings
without paying for the review twice.

### `codex_ask` — the same thing for questions

```python
from lazytools.connectors.code_support import codex_consultant

tool = codex_consultant(root=...)   # -> Tool named "codex_ask"
await tool.run(question="does thread/resume keep dynamic tools?", repo_path="LazyBridge")
```

Same engine, same confinement, same threading; a different role. `CODE_CONSULTANT_SYSTEM`
asks for a *design answer* — separating what was verified in the source from
what is inferred, and preferring "I don't know, here is the experiment that
settles it" over a confident guess. The reviewer prompt is the wrong instrument
here: asked a question, it replies with a findings list.

Both are what the [MCP server](../mcp-server.md#code_review-hand-a-review-to-codex)
mounts as provider `code_review`, which is how another coding agent (Claude
Code, say) gets Codex as a second reviewer *and* a second opinion.

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
