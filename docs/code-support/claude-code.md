# Claude Code

The current write/review tools use `ClaudeCodeEngine` through the Claude Agent
SDK. Expose `claude_review code_write code_sessions` through
[LazyTools MCP](../mcp-server.md), or construct the factories in Python.
Install `[engines,mcp]` as shown in the [overview](index.md).

| MCP tool | Python factory/provider | Important arguments |
| --- | --- | --- |
| `claude_code_review` | `claude_reviewer()` | `task`, `repo_path`, `diff_ref`, `paths`, `session_id`, `session_name`, `model`, `effort` |
| `claude_ask` | `claude_consultant()` | `question`, `repo_path`, `session_id`, `session_name`, `model`, `thinking`, `effort` |
| `claude_code_write` | `CodeWriteTools(base_dir=...)` | `task`, `cwd`, `session_id`, `session_name`, `model`, `effort` |

`paths` is a comma-separated string. `model` defaults to `sonnet` in the
engine-backed reviewer/consultant. Effort accepts `low`, `medium`, `high`,
`xhigh`, `max`; runtime/model support can differ. Replies carry the native
`session_id` and supplied alias. Reuse the alias in the same repository to
continue; it cannot resume a Codex thread.

```python
import asyncio
from pathlib import Path
from lazytools.connectors.code_support import claude_consultant, claude_reviewer

root = str(Path.cwd())
async def inspect():
    review = claude_reviewer(root=root, web=False)
    print(await review.run(task="Review deployment.py for weekend boundary defects.",
                           paths="deployment.py", session_name="deployment-review"))
    ask = claude_consultant(root=root, web=False)
    print(await ask.run(question="Which boundary test would detect that defect?",
                        session_name="deployment-review", model="opus", effort="high"))

asyncio.run(inspect())
```

Run from a repository containing `deployment.py`, such as the
[toy repository](../code-bridge.md#claude-writes-codex-reviews). Reviewers get
scoped `Read`/`Glob`/`Grep` and Git tools; no Bash or file edits. Web access is
on by default unless `web=False`. Consultants can receive additional tools;
the MCP provider supplies its configured read-only toolset.
Failures raise `ReviewNotPerformed`, including timeouts and turn-cap failures.

Writers grant `Write`/`Edit` and optionally Bash, with a 60-turn cap and no
automatic retries. File tools are confined to resolved `cwd`; Bash needs its
own policy. Direct writers require `confirm_write()` per call by default.
See the [write example](index.md#claude-writes-codex-reviews).

## CLI and raw MCP wrappers

`claude_code(task, mode="read", ...)` is the separate subprocess wrapper
around `claude -p --output-format json`, with read/plan modes only.
It accepts native `session_id`, not `session_name`, and defaults to
`claude-sonnet-5-5`. Read mode grants `Read,Grep,Glob` without Bash.
Success returns a labelled untrusted result; connector failures return a
`[claude_code] ...` string.

`claude_code_mcp(allow=...)` wraps `claude mcp serve`: the host calls raw
file/shell tools; no Claude model reviews the request. Names depend on the
installed runtime. The client owns confirmation for writes. Use a narrow
namespaced allow-list.

For subprocess wrappers, let their `timeout` own the deadline. Inside an
`LLMEngine`, set `tool_timeout=None` or larger than the wrapper's timeout:
cancelling a Python worker thread does not stop its subprocess.
Authentication stays with the runtime and inherited environment.
Use [the code bridge](../code-bridge.md) for durable approvals and async work.

## Explicit timeout configuration

The CLI wrapper's timeout is 3600 seconds. This runnable helper forwards the
deadline; engine-backed reviewer/write timeouts are factory/provider settings:

```python
from lazytools.connectors.code_support import claude_code

def read_with_claude(
    task: str,
    timeout: float = 3600.0,
):
    return claude_code(task, timeout=timeout)
```

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `timeout` | `float` | `3600.0` | Maximum seconds for the CLI subprocess. |
