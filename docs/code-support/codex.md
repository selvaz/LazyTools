# Codex

The current write/review tools use `CodexEngine` over JSON-RPC to the local
`codex app-server`. Authentication and defaults come from that runtime.
Expose `code_review code_write code_sessions` through [LazyTools MCP](../mcp-server.md).

| MCP tool | Python factory/provider | Purpose |
| --- | --- | --- |
| `codex_code_review` | `codex_reviewer()` | Prompted defects review: `task`, `repo_path`, `diff_ref`, `paths` |
| `codex_ask` | `codex_consultant()` | Design question: `question`, `repo_path` |
| `codex_review_changes` | `codex_native_reviewer()` | Native harness: `scope`, `ref`, `repo_path`; no task prompt |
| `codex_write` | `CodeWriteTools(base_dir=...)` | Write: `task`, `cwd` |

All accept `model`, `effort`, `session_name`. Prompted review, ask and write
also accept `thread_id`. `paths` is a comma-separated string.
The harness takes `scope="uncommitted"`, `scope="branch", ref="main"`,
or `scope="commit"` with a commit ref; there is no prompt slot to steer it.

```python
import asyncio
from pathlib import Path
from lazytools.connectors.code_support import codex_consultant, codex_native_reviewer

root = str(Path.cwd())
async def inspect():
    review = codex_native_reviewer(root=root)
    print(await review.run(scope="uncommitted", session_name="deployment-review",
                           model="gpt-6.1-sol", effort="high"))
    ask = codex_consultant(root=root)
    print(await ask.run(question="Which boundary test would detect these findings?",
                        session_name="deployment-review"))

asyncio.run(inspect())
```

Run from a Git repository with a bounded change. Review/ask tools use a
read-only sandbox, `approval_policy="never"`, and web access by default.
The consultant can receive dynamic tools; the MCP provider supplies its
configured read-only toolset. Failed turns raise `ReviewNotPerformed` and
appear as MCP errors, never successful findings.

## Naming a conversation: `session_name`

Reply headers carry `thread_id` and the supplied alias. Reuse
`session_name="deployment-review"` in the same repository to resume;
an explicit `thread_id` wins and rebinds the name. Manage names using
[`code_sessions_*`](index.md#name-a-native-conversation). Native history stays
engine-specific; a Claude transcript cannot become a Codex thread.
`codex_write` no longer accepts `resume_last`.

Code-support tools validate the effort vocabulary (`none`, `minimal`, `low`,
`medium`, `high`, `xhigh`, `max`, `ultra`); the selected runtime/model determines
actual support. Bridge routing additionally validates known model-specific
sets, such as Luna's lack of `ultra`. Defaults come from the runtime unless
factory/per-call overrides are given. The default request timeout is 3600
seconds; configure the host timeout for that duration.

Writers use workspace-write and retain the Git repository check by default.
Direct `CodeWriteTools` calls need one-shot confirmation; the opt-in MCP writer
has no per-call confirmation. See the [write example](index.md#claude-writes-codex-reviews).

## CLI and native MCP wrappers

`codex(task, ...)` separately wraps `codex exec` in a subprocess.
It is read-only, with `resume_last` for CLI history; it takes neither `mode`
nor `thread_id`. It does not provide the writer's `session_name` alias
interface. Success returns a labelled untrusted result; errors return a
`[codex] ...` string.

`codex_mcp(allow=...)` wraps `codex mcp-server` and its native `codex` /
`codex-reply` tools through the [MCP connector](../mcp.md). It requires an
explicit filter matching namespaced names. This differs from serving
LazyTools tools through `lazytools-mcp`.
Use [detached bridge jobs](../code-bridge.md) for long tasks and approval tickets.

## Explicit timeout configuration

The wrapper's timeout is 3600 seconds. This runnable helper forwards it to
the CLI; the reviewer factory takes the same default at construction:

```python
from lazytools.connectors.code_support import codex, codex_reviewer

def read_with_codex(
    task: str,
    timeout: float = 3600.0,
):
    return codex(task, timeout=timeout)

def reviewer_for(
    root: str,
    timeout: float = 3600.0,  # seconds per review
):
    return codex_reviewer(root=root, timeout=timeout)
```

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `timeout` | `float` | `3600.0` | CLI subprocess deadline; reviewer request deadline is configured on the factory. |
