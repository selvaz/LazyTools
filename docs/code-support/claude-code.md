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

## Continuing a review or consultation

The successful header carries `session_id=<repo>#<native-id>`, scoped to the
resolved repo relative to the configured root. Pass that complete handle to
the reviewer or consultant to retain Claude's native history. A handle for a
different repo is refused. An explicit `session_id` wins over `session_name`
and rebinds the alias; aliases are managed in the shared session registry.

```python
import asyncio
import re
from pathlib import Path
from lazytools.connectors.code_support import claude_consultant, claude_reviewer

async def continue_review():
    root = str(Path.cwd())
    reviewer = claude_reviewer(root=root, web=False)
    first = await reviewer.run(task="Review the uncommitted timeout handling.")
    handle = re.search(r"session_id=([^\s]+)", first).group(1)
    consultant = claude_consultant(root=root, web=False)
    print(await consultant.run(
        question="Which independent test would falsify the concern?",
        session_id=handle, session_name="claude-followup",
    ))

asyncio.run(continue_review())
```

Review and consultation share permissions and continuation mechanics but have
different roles: a reviewer reports Bugs, Risks and Cleanups; a consultant
answers a question and distinguishes verified source from inference.
`web=False` removes web tools and their prompt addendum. Default reviewer and
consultant turn caps are 60, with a 3600-second request ceiling. Neither a
timeout nor a turn-cap failure can stand in for a performed review.

`ReviewNotPerformed` retains `reason`, `handle`, `cwd`, `label` and
`session_name`. Catch it in an orchestrator to record failure and decide
whether to resume; do not catch it and turn the failure into empty findings.

## Writing: `claude_code_write`

`CodeWriteTools(base_dir=...)` emits the Claude writer by default. Every call's
`cwd` must resolve inside the existing root; `default_cwd` can select a
subdirectory. `Write`/`Edit` hooks are confined to that resolved cwd,
not just the wider base root. Bash is a separate capability and is not
path-confined; `claude_bash=False` removes it.

Each `confirm_write()` grants one call. `confirm_write(task_id=...)` scopes a
grant to that running task; an invalid cwd is rejected before consuming it.
The opt-in MCP provider uses `require_confirmation=False`, so exposing writes
is the host's explicit capability decision. Native `session_id`, an alias,
model and effort can continue a writer conversation; no automatic retry is
made after failure or maximum turns.

Writer headers carry native session ids; review/ask headers add `<repo>#`.
Use a common repository-scoped alias between the tools, or deliberately
extract the native id before continuing a reviewer conversation in a writer.
Review/ask still accept bare native ids for compatibility; that loses the
repository component's cross-check.

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

### CLI arguments

| Argument to `claude_code` | Default | Meaning |
| --- | --- | --- |
| `task` | Required | One delegated task |
| `mode` | `"read"` | `read` grants Read/Grep/Glob; `plan` selects CLI plan mode; no `write` argument |
| `cwd` | `None` | Subprocess working directory |
| `session_id` | `None` | Native CLI id passed to `--resume`; distinct from an engine-backed repository handle |
| `model` | `"claude-sonnet-5-5"` | Wrapper default; engine-backed factories instead default to `sonnet` |
| `effort` | `None` | Optional per-call override |

The CLI reads stdin from `DEVNULL` so an unattended trust/auth prompt cannot
wait forever for terminal input. Complete any required interactive runtime
setup before using the wrapper; inherited credentials stay with the runtime.
Success returns `result` and untrusted-content labels; those labels identify
repository-derived text and do not grant approval for an action.

### Discovering the live tool surface

Raw tools belong to the installed Claude Code version. Discover names and
schemas through `alist_tools()` without calling a model or file tool, then
construct a narrowly filtered server for normal use:

```python
import asyncio
from lazytools.connectors.code_support import claude_code_mcp

async def discover_claude():
    async with claude_code_mcp(allow=["*"]) as server:
        print([(tool.name, tool.schema_llm) for tool in await server.alist_tools()])
    async with claude_code_mcp(allow=["claude_code.Read", "claude_code.Glob",
                                     "claude_code.Grep"]) as server:
        print([tool.name for tool in await server.alist_tools()])

asyncio.run(discover_claude())
```

The factory is lazy until discovery. Namespacing defaults to `<name>.`;
filters match those names, so `Read` alone will not match `claude_code.Read`.
`name`, `prefix`, `namespace`, `args`, `env` and `cache_tools_ttl` configure
the transport/factory. Cache TTL defaults to 60 seconds; invalidation causes
rediscovery. A closed server is single-use: create another after its context
manager exits. This uses public discovery rather than `agent._tool_map`.

Do not assume every runtime advertises `Read`, `View`, `LS` or other historical
names. Use the actual schema. A deny list containing only Bash still permits
other advertised write tools; it is not a read-only configuration.
The raw MCP client owns confirmation of edits and shell calls. There is no
delegated Claude reviewer role or plan-mode gate on these raw primitives.

## Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `[claude_code] CLI 'claude' not found` | Executable absent from PATH | Run `check_clis_available()` and configure the runtime environment. |
| Auth-related CLI/SDK failure | Runtime credentials unavailable | Sign in using the intended runtime account; use its supported credential setup. |
| Timeout or maximum-turn error | Request deadline/turn budget exhausted | Record failure; scope or resume deliberately rather than marking review performed. |
| CLI subprocess survives outer timeout | Cancelling a worker thread does not kill its subprocess | Set `LLMEngine.tool_timeout=None` or strictly above the wrapper timeout. |
| `CodeWriteBlocked` | Missing one-shot grant or cwd outside root | Check the reason and the intended capability before granting another call. |
| File tool denied | Resolved file escapes call cwd | Use the correct repo/cwd; Bash has a separate policy and does not repair file confinement. |
| `session_id` malformed or wrong repository | Confused native id/engine-backed handle or copied punctuation | Use the full successful header value on the engine tools and a native id on the CLI wrapper. |
| Raw MCP factory requires filter | Missing `allow`/`deny` | Discover the surface, then supply namespaced filters. |
| MCP/Agent SDK import error | Missing optional dependencies | Install `[engines,mcp]` for the release shown in the overview. |

## Pitfalls

- Raw MCP tools expose file/shell primitives without a Claude model; they
  do not perform a review simply because they are served by Claude Code.
- File hooks and a checked cwd do not confine Bash. Remove or govern that
  capability when the task does not require it.
- The CLI subprocess returns labelled text or a connector error string;
  engine-backed reviews raise `ReviewNotPerformed`. Handle both contracts.
- Model and effort support depends on the installed runtime; validate the
  desired combination before starting an expensive turn.
- A resumed conversation may contain previous conclusions. Share the current
  diff and independently run checks when recording acceptance.

## See also

- [Code support overview](index.md) for installation, startup and permissions.
- [Codex](codex.md) for native diff review and opposite-engine consultation.
- [Collaboration](collaboration.md) for the integration choices.
- [Async code bridge](../code-bridge.md) for detached jobs and approval tickets.
- [MCP connector](../mcp.md) for discovery, caching and transport lifecycle.
- [Safety](../safety.md) for client-side capability gates.
