# Code support: Claude Code + Codex

LazyTools makes the two coding runtimes practical collaborators: delegate a
write, request a review from the other engine, and keep native conversations
under names. The synchronous MCP tools use LazyBridge's `CodexEngine`
(App Server) and `ClaudeCodeEngine` (Agent SDK). For long jobs and durable
approval tickets, use the [async code bridge](../code-bridge.md).

**LazyTools 0.10.5 is Alpha**, with `lazybridge>=1.8.1,<2.0`.
The distribution is `lazytoolkit`; imports use `lazytools`.

```sh
python -m pip install "lazytoolkit[engines,mcp] @ git+https://github.com/selvaz/LazyTools.git@v0.10.5"
lazytools-mcp projects code_review claude_review code_write code_sessions --allow-unsafe
```

Sign in to the local runtimes first. With a subscription login they use your
plan's quota; adapters inherit runtime authentication and do not force a
billing path. Configure the [code root and project Store](../mcp-server.md#collaboration-setup)
before connecting a host. Coding providers are opt-in even for reviews because
each call runs a model turn. `--allow-unsafe` exposes writes without interactive
confirmation; it is separate from bridge approval tickets.

## Tools to connect the work

| Provider | Tools | Purpose |
| --- | --- | --- |
| `code_review` | `codex_code_review`, `codex_ask`, `codex_review_changes` | Focused review, design questions, native typed diff review |
| `claude_review` | `claude_code_review`, `claude_ask` | Focused review and design questions |
| `code_write` | `codex_write`, `claude_code_write` | File edits and command execution |
| `code_sessions` | `code_sessions_list`, `code_sessions_bind`, `code_sessions_rename`, `code_sessions_forget` | Inspect and manage conversation aliases |

All seven engine-backed review/ask/write tools accept per-call `model`,
`effort` and `session_name`. Codex also accepts `thread_id` on prompted
review, ask and write; Claude accepts `session_id`. `codex_review_changes`
takes `scope` (`uncommitted`, `branch`, `commit`) and `ref`, with no prompt slot.
There is no Claude native review-harness counterpart.

## Claude writes, Codex reviews

The [worked bridge flow](../code-bridge.md#claude-writes-codex-reviews) creates
a tiny repository and exercises the job lifecycle. For a short synchronous
turn, Python calls the same factories that MCP exposes. Run this from an
existing Git repository (the current directory is the code root):

```python
import asyncio
from pathlib import Path
from lazytools.connectors.code_support import CodeWriteTools, codex_reviewer

root = str(Path.cwd())
writer = CodeWriteTools(base_dir=root)
write = next(tool for tool in writer.as_tools() if tool.name == "claude_code_write")
reviewer = codex_reviewer(root=root)

async def collaborate():
    writer.confirm_write()  # explicitly grant one write call
    draft = await write.run(task=
        "Add deployment_allowed(day): allow weekdays, block weekends. Add boundary tests.",
        cwd=root, model="sonnet", effort="high", session_name="deployment-writer",
    )
    print(draft)
    review = await reviewer.run(
        task="Review the uncommitted deployment change and boundary tests; report defects.",
        repo_path=root, model="gpt-6.1-sol", effort="high", session_name="deployment-review",
    )
    print(review)

asyncio.run(collaborate())
```

This wiring explicitly chooses different engines. MCP does not automatically
prevent arbitrary callers from reviewing with the producing engine. Keep the
diff, findings and independently executed checks before recording
[project acceptance](../projects.md#contracts-and-verification).
A review that cannot run raises `ReviewNotPerformed` in Python and becomes an
MCP error result; it is not a clean review.

## Name a native conversation

Use `session_name="deployment-review"` again in the same repository to resume.
Aliases are keyed by kind (`codex` / `claude`), resolved repository and name, in
`~/.lazybridge/sessions.json` or `LAZYBRIDGE_SESSIONS_FILE`.
Explicit native ids take precedence and rebind a supplied alias.

```python
from pathlib import Path
from lazytools.connectors.code_support import CodeSessionTools

sessions = CodeSessionTools(root=str(Path.cwd()), allow_mutate=True)
print(sessions.code_sessions_list())
sessions.code_sessions_bind("codex", "review", "existing-thread-id")
sessions.code_sessions_rename("codex", "review", "adopted-review")
sessions.code_sessions_forget("codex", "adopted-review")
```

Replace `existing-thread-id` with an id from a real reply before using the
bound name for a turn. Binding does not create or validate a native thread;
rename/forget affect only the alias. Claude transcripts and Codex threads
remain engine-specific. Bridge `--session` names pin subsequent bridge jobs
in that repository to the original engine.

## Permissions and timeouts

### Startup check

```python
from lazytools.connectors.code_support import check_clis_available

available = check_clis_available()
print(available)  # {"claude": bool, "codex": bool}
```

This checks executable resolution, not authentication or quota. Claude is
looked up on PATH; Codex uses LazyBridge's resolver, including the desktop
app install directory and `CODEX_BIN`. Sign in and test the intended engine
before a live task. The engine-backed Claude path also needs the Agent SDK;
raw MCP needs the official MCP SDK. Discovery and a model turn are different
checks: raw MCP tool listing does not prove an authenticated review can run.

Codex reviewers use a read-only sandbox and `approval_policy="never"`.
Claude reviewers get scoped `Read`/`Glob`/`Grep` plus supplied Git tools;
they have no Bash. Reviewers can access the web by default. Writers are a
separate capability: direct `CodeWriteTools` calls require one-shot grants by
default; the MCP provider disables per-call confirmation. Claude file hooks
confine file tools to `cwd`; Bash is not path-confined.
`CodeWriteTools(claude_bash=False)` removes Bash. Codex writes retain the Git
repository check. Write results label repository-derived content as untrusted;
failed writes are not automatically retried.

Calls block until the delegated turn finishes. The default request timeout is
3600 seconds; set the MCP host timeout accordingly (Claude Code's
`MCP_TOOL_TIMEOUT` uses milliseconds). Prefer detached bridge jobs for long
work, approval queues and independently restartable waiters.

### Outer timeout for CLI tools

When a CLI wrapper is an `LLMEngine` tool, use `tool_timeout=None` or a
strictly larger outer deadline than its subprocess `timeout`. The synchronous
function runs in a worker thread; cancelling `asyncio.wait_for` does not stop
that thread or its subprocess. Let the subprocess own its deadline, for example
a 300-second wrapper budget with an outer ceiling greater than 300. Engine
reviewers/writers instead set their request timeout on the factory/provider.
The MCP host must also allow enough time for the call and transport overhead.

### Write grants and output contracts

`confirm_write(task_id=...)` can scope a one-shot grant to a task so another
concurrent task cannot spend it. Every invalid cwd is rejected before consuming
a grant. Direct `CodeWriteTools` defaults to Claude only; set `codex=True`
deliberately to expose the Codex writer. `default_cwd` selects the default
subdirectory within `base_dir`; `codex_skip_git_check` defaults to `False`.

CLI wrappers return `{"result": ..., "content_is_untrusted": true}` on
success, or a `[claude_code] ...` / `[codex] ...` error string.
Engine-backed reviewers/consultants return header plus findings/answer and
raise `ReviewNotPerformed` on a failed turn. Writers return labelled result or
failure text. None of these output shapes is an acceptance verdict.

## Other integration surfaces

The older CLI wrappers still work: `claude_code` supports read/plan tasks,
while `codex` is read-only. `claude_code_mcp` exposes Claude's raw file/shell tools;
`codex_mcp` exposes Codex's native `codex` / `codex-reply` tools. These use the
[MCP connector](../mcp.md), require explicit `allow=` / `deny=` filters and
differ from the engine-backed LazyTools MCP tools above. See the
[Claude guide](claude-code.md), [Codex guide](codex.md) and
[collaboration guide](collaboration.md).

## Troubleshooting and pitfalls

| Check | What it resolves |
| --- | --- |
| `check_clis_available()` | Executable lookup; it does not inspect credential files |
| Raw MCP `alist_tools()` | Installed tool names/schemas and transport connection |
| Factory/provider settings and host timeout | Default 3600-second engine request versus host cancellation |
| `CodeWriteBlocked` reason and selected tool menu | Missing grant, cwd escape or writer not exposed |
| `ReviewNotPerformed` / MCP `isError=True` | Review failed structurally; do not record it as performed |
| Native id/alias and repository | Engine-specific history; ids cannot cross repositories or engines |

Use the engine-specific [Codex](codex.md#troubleshooting) and
[Claude](claude-code.md#troubleshooting) tables for recovery details. The raw
MCP interfaces evolve with the runtime: discover before relying on a schema.
Namespaced filters govern exposed capabilities; a broad deny list can still
leave write tools available. Repository text is untrusted input, and scoped
parameters do not constrain arbitrary instructions embedded in that text.

## See also

- [Codex](codex.md) for prompted/native reviews, questions, writes and continuation.
- [Claude Code](claude-code.md) for SDK and CLI/raw MCP behavior.
- [Collaboration](collaboration.md) for selecting an integration path.
- [Async code bridge](../code-bridge.md) for the complete toy-repository flow.
- [Projects](../projects.md) for shared plans, contracts and verification.
- [MCP connector](../mcp.md) and [Safety](../safety.md) for transports and gates.
