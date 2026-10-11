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

## Prompted review and `thread_id` continuation

`codex_reviewer(root=...)` fixes the reviewer role and returns a Tool named
`codex_code_review`. The caller supplies the repository and review question;
the engine reads source and Git evidence itself. `diff_ref="main"` requests
the branch comparison plus uncommitted changes; `paths` narrows the scope.
The report separates Bugs, Risks and Cleanups, with concrete failure scenarios.

`repo_path` must resolve inside `root` (explicit root, `LAZYTOOLS_CODE_ROOT`,
then process cwd); each `paths` entry must remain inside the selected repo.
The free-text task is an instruction, not a structural read boundary.
Read-only permissions prevent patches but do not prove a review covered every
file or establish a general host read sandbox.

Each reply header carries `thread_id=<repo>#<native-id>`. Keep the complete
handle and pass it back to continue a review or ask about its findings:

```python
import asyncio
import re
from pathlib import Path
from lazytools.connectors.code_support import codex_consultant, codex_reviewer

async def continue_review():
    root = str(Path.cwd())
    reviewer = codex_reviewer(root=root)
    first = await reviewer.run(task="Review the uncommitted retry logic.", repo_path=root)
    handle = re.search(r"thread_id=([^\s]+)", first).group(1)
    followup = await reviewer.run(
        task="Check the timeout interaction against your earlier findings.",
        repo_path=root, thread_id=handle,
    )
    consultant = codex_consultant(root=root)
    answer = await consultant.run(
        question="Which independent test would falsify that concern?",
        repo_path=root, thread_id=handle,
    )
    print(first, followup, answer, sep="\n\n")

asyncio.run(continue_review())
```

The repository component fences a resumed handle against another checkout;
copying a different repo's handle is refused. Native history belongs to Codex;
the resumed engine does not send a second LazyBridge Memory chronology.
Aliases avoid manually carrying handles, while explicit `thread_id` takes
precedence and rebinds a supplied alias. A failed turn raises
`ReviewNotPerformed`, with `reason`, `handle`, `cwd`, `label`, `session_name`;
it cannot serve as a performed clean review.

## `codex_review_changes`: the native review harness

`codex_native_reviewer()` calls App Server `review/start` with a typed target:
`uncommitted` includes staged, unstaged and untracked work; `branch` requires
its base `ref`; `commit` requires a commit `ref`. The scope is the instruction:
there is no `task` or `question` argument. Model, effort and `session_name`
remain configurable, but this tool does not take an explicit `thread_id` input.

| Choice | `codex_code_review` | `codex_review_changes` |
| --- | --- | --- |
| Instruction | Caller task and reviewer system | Native typed diff target |
| Standards | Bugs, Risks, Cleanups contract | Installed Codex harness |
| Best fit | A specific correctness question | A branch/commit reviewed on its own merits |
| Continuation | Explicit handle or alias | Alias for review; returned handle can go to `codex_ask` |

The wrapper returns severity-tagged findings as text, not a `findings[]`
array. Only inline delivery is wired; this is not the bridge's detached-job
mechanism. The earlier guide's codex-cli 0.148.0 observation was a historical
smoke check, not a version requirement or a current quality benchmark.

## `codex_ask`: questions over source and findings

`codex_consultant()` shares the engine, path checks and continuation semantics,
but uses a design-answer role rather than forcing a findings list. It asks the
engine to distinguish source-verified facts from inference and identify an
experiment when uncertain. Pass the native review's returned `thread_id` or
its alias to interrogate findings without rerunning the entire review.
Programmatic `tools=` and MCP's configured consultant toolset add capabilities;
grant only the read-only tools appropriate to the question.

## `codex_write`: opt-in edits

The direct provider defaults to Claude only: construct
`CodeWriteTools(base_dir=..., claude=False, codex=True)` to emit `codex_write`.
It accepts `task`, `cwd`, `thread_id`, `session_name`, `model`, `effort`.
An omitted `cwd` uses `default_cwd` or `base_dir`; both are checked against
the root on every call. Confirmation is a capability held by the caller,
not an argument the delegated model can grant itself.

```python
import asyncio
from pathlib import Path
from lazytools.connectors.code_support import CodeWriteTools

root = str(Path.cwd())  # run from a Git repository
writer = CodeWriteTools(base_dir=root, claude=False, codex=True)
write = next(tool for tool in writer.as_tools() if tool.name == "codex_write")

async def implement():
    writer.confirm_write()
    print(await write.run(
        task="Add a focused timeout boundary test; do not commit.", cwd=root,
        session_name="codex-implementation", model="gpt-6.1-sol", effort="high",
    ))

asyncio.run(implement())
```

The tool checks Git itself because App Server has no CLI
`--skip-git-repo-check` flag. `codex_skip_git_check=True` is an explicit
provider override for disposable directories. Resuming with a known alias
or the writer's native `thread_id` replaces the former writer `resume_last`; the CLI read wrapper
still has that separate option. Writes are never automatically retried.

Writer headers carry native ids directly; review/ask headers add `<repo>#`.
Use a shared repository-scoped alias to continue between those tools, or
extract the native id deliberately before passing a review handle to a writer.
Review/ask accept bare native ids for compatibility, but those lose the
repository component's cross-check; retaining the complete handle is preferred.

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

### CLI arguments

| Argument to `codex` | Default | Meaning |
| --- | --- | --- |
| `task` | Required | One read-only delegated task |
| `cwd` | `None` | Subprocess working directory |
| `resume_last` | `False` | Resume most recent CLI conversation, with its ambiguity |
| `skip_git_check` | `True` | CLI read wrapper may inspect a non-Git folder |
| `model`, `effort` | `None` | CLI configuration overrides |

### Discovering the live tool surface

Native MCP tool names and schemas belong to the installed runtime. Discover
them through the public `alist_tools()` API, then create a new server with a
narrow allow-list. Merely constructing the factory is lazy; discovery makes
the connection. This replaces the old private `agent._tool_map` inspection.

```python
import asyncio
from lazytools.connectors.code_support import codex_mcp

async def discover_codex():
    async with codex_mcp(allow=["*"]) as server:
        advertised = await server.alist_tools()
        print([(tool.name, tool.schema_llm) for tool in advertised])
    # Discovery above calls no model tool. Use observed names for the host.
    async with codex_mcp(allow=["codex.codex", "codex.codex-reply"]) as server:
        print([tool.name for tool in await server.alist_tools()])

asyncio.run(discover_codex())
```

The commonly advertised primitives are `codex` (new conversation) and
`codex-reply` (continuation with runtime `threadId`), namespaced by default.
Their schemas differ from LazyTools' `codex_ask`/`codex_write` tools.
The default discovery cache is 60 seconds; `invalidate_tools_cache()` clears
it, and `cache_tools_ttl=None` keeps it until invalidation. Closing is terminal:
construct a new server after `aclose()` or exiting the context manager.

## Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Codex executable unavailable | CLI/app install not resolved | Run `check_clis_available()`; set `CODEX_BIN` to the executable if needed. The resolver also checks the desktop app install directory. |
| CLI error outside Git | `skip_git_check=False` | Read wrapper defaults to `True`; writes intentionally keep their separate Git rail. |
| `CodeWriteBlocked` | No grant or outside-root cwd | Inspect the reason; grant one authorized call and use an existing directory inside `base_dir`. |
| Writer returns a Git-rail refusal | Checkout has no Git repository | Initialize/use the intended checkout; `codex_skip_git_check=True` is only an explicit disposable-directory override. |
| `ReviewNotPerformed` / MCP `isError=True` | Turn failed, timed out or could not execute | Retain the handle and error; fix the cause before requesting another review. |
| Timeout after 3600 seconds | Request exceeds configured deadline | Set the factory/provider timeout and host timeout together; use detached bridge jobs for long work. |
| CLI keeps running after outer cancellation | `LLMEngine.tool_timeout` shorter than subprocess timeout | Use `tool_timeout=None` or a strictly larger outer limit. |
| Explicit-filter `ValueError` | Raw MCP factory lacks `allow`/`deny` | Discover the surface, then supply namespaced filters. |
| Optional SDK import error | Missing `[mcp]` extra | Install the release extras shown in the overview. |
| Malformed/wrong-repo handle | Punctuation copied from error or other checkout | Copy the complete successful header value, or use the repository-scoped alias. Bare ids bypass the repository-component cross-check. |

## Pitfalls

- CLI `resume_last` selects the most recent session in a directory. Use an
  engine-backed alias when conversations run in parallel.
- Native MCP `threadId`, CLI history, and LazyTools `thread_id` handles are
  different surfaces; use each tool's discovered schema.
- A supplied consultant toolset broadens its authority; read-only repository
  policy is not a guarantee about every external tool supplied by the caller.
- Model/effort support comes from the installed runtime. An accepted effort
  spelling does not guarantee that every model supports that effort.
- A clean findings string is evidence of a completed review, not project
  acceptance; acceptance requires the recorded checks and caller policy.

## See also

- [Code support overview](index.md) for startup, permissions and session tools.
- [Claude Code](claude-code.md) for the other engine's review/ask/write surface.
- [Collaboration](collaboration.md) for choosing the integration path.
- [Async code bridge](../code-bridge.md) for jobs and approval tickets.
- [MCP connector](../mcp.md) for transport lifecycle and filtering.
- [Projects](../projects.md#contracts-and-verification) for acceptance evidence.
