# Collaboration

`build_cli_collaboration` makes **Claude Code and Codex work together** on a
task — *Claude Code analyses, Codex critiques, a synthesizer plans, an executor
implements* — packaged as a **single tool**, the same way you'd pass
[`claude_code`](claude-code.md) or [`codex`](codex.md).

```python
from lazytools.connectors.code_support import build_cli_collaboration

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

**Default: three sessions, nothing is written** — two read-only CLI sessions
plus one synthesizer that writes the *plan*, not code:

```
Step 1  claude_analyst   claude_code(read)    analyse, propose an approach
Step 2  codex_analyst    codex (read-only)    critique/confirm  (sees step 1 via shared Memory)
Step 3  synthesizer      —                    merge into one concrete written plan
Step 4  executor         claude_code_write    implement the plan   (ONLY with execute=True + base_dir=)
```

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | `"cli_collaboration"` | Tool name the parent agent sees (tool-map key). |
| `description` | `str \| None` | `None` | Tool description shown to the parent LLM; a sensible default is used when `None`. |
| `claude_model` | `str` | `"claude-opus-4-8"` | Model for the Claude-Code analyst (step 1). |
| `codex_model` | `str` | `"gpt-5.4"` | Model for the Codex analyst/critic (step 2). |
| `synthesizer_model` | `str` | `"claude-opus-4-8"` | Model that merges the analyses (step 3). |
| `executor_model` | `str` | `"claude-opus-4-8"` | Model that implements the plan (step 4). |
| `execute` | `bool` | `False` | `False` (default) → stop after synthesis: the read-only three-session pipeline. `True` → append the executor, which implements the plan via the gated `claude_code_write` tool. |
| `base_dir` | `str \| None` | `None` | **Required when `execute=True`**: the sandbox root the executor may write inside (ideally a git checkout). |
| `writer` | `CodeWriteTools \| None` | `None` | Bring your own writer for the executor (mutually exclusive with `base_dir`). This is the only way to run a **gate-enabled** executor: you hold the instance, so you can call `writer.confirm_write()` per executor write while the pipeline runs. |

## Examples

=== "Analyse + plan (default)"

    ```python
    from lazytools.connectors.code_support import build_cli_collaboration

    # The default three-session pipeline: two read-only CLI analysts plus a
    # synthesizer that writes the plan. Nothing on disk is modified.
    planner = build_cli_collaboration()
    print(planner("Propose a refactor of the payments module").text())
    ```

=== "As a sub-tool"

    ```python
    from lazybridge import Agent, LLMEngine
    from lazytools.connectors.code_support import build_cli_collaboration

    # Hand the collaboration to a higher-level orchestrator, alongside any
    # other tools — it behaves like any other tool that takes a task string.
    orchestrator = Agent(
        engine=LLMEngine("claude-opus-4-8"),
        tools=[build_cli_collaboration(name="deep_code_task")],
    )
    orchestrator("Use deep_code_task to add retries to the HTTP client")
    ```

=== "Implement (opt-in)"

    ```python
    from lazytools.connectors.code_support import build_cli_collaboration

    # execute=True appends the executor: the plan is implemented through the
    # gated claude_code_write tool, sandboxed to base_dir (use a git checkout).
    pipeline = build_cli_collaboration(execute=True, base_dir="/path/to/project")
    print(pipeline("Add rate limiting to the /api/login endpoint").text())
    ```

## Why `Plan`, not `AgentPool`?

The flow is fixed and sequential (analyse → critique → synthesise [→ execute]).
`Plan` with `from_step` is simpler and more predictable, and each step frees
memory before the next. Reach for `AgentPool` only when you need dynamic routing
or a multi-round back-and-forth.

## Pitfalls

- **The default never writes.** `execute=True` requires `base_dir=` and
  routes all writes through the `CodeWriteTools` sandbox; keep `base_dir` a
  git checkout so every change is reviewable.
- **Shared `Memory` is sequential-only.** The pipeline's `dialogue` memory is
  safe because `Plan` steps don't overlap; don't reuse the pattern under
  parallel execution without a per-agent memory.
- **Both CLIs must be installed and authenticated** — the pipeline drives
  `claude_code` and `codex` in CLI mode. Run `check_clis_available()` first.

## See also

- [Claude Code](claude-code.md) — the analyst/executor side.
- [Codex](codex.md) — the critic side.
- [Code Support Agent](index.md) — install, modes, timeouts, startup check.
