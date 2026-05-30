# Supervisor REPL (`SupervisorEngine`)

`SupervisorEngine` is the **REPL human**: an interactive prompt the person drives
to supervise a pipeline — approve and continue, retry a named sub-agent with
feedback, call tools, and read the store, all with an audit trail.
`supervisor_agent(...)` is the sugar that builds an `Agent` around it.

> Part of [Human-in-the-loop](index.md). Ships in `lazybridge.ext.hil`.

## Signature

```python
from lazybridge import Agent
from lazybridge.ext.hil import SupervisorEngine, supervisor_agent

# Engine — pass to Agent(engine=...)
SupervisorEngine(
    *,
    tools=None,        # list[Tool | Callable] — callable from the REPL as tool(args)
    agents=None,       # list[Agent] — retry targets for `retry <agent>: <feedback>`
    store=None,        # Store — readable from the REPL via `store <key>`
    input_fn=None,     # Callable[[str], str] — sync prompt source (default: input())
    ainput_fn=None,    # Callable[[str], Awaitable[str]] — async prompt source
    timeout=None,      # float | None — per-prompt wait before falling back
    default=None,      # str | None — value used on timeout
)

# Sugar — returns a ready Agent(engine=SupervisorEngine(...))
supervisor_agent(
    *,
    tools=None, agents=None, store=None,
    input_fn=None, ainput_fn=None, timeout=None, default=None,
    **agent_kwargs,    # name=, session=, description=, …
) -> Agent
```

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `tools` | `list[Tool \| Callable] \| None` | `None` | Tools the human can invoke from the REPL as `tool(args)`. |
| `agents` | `list[Agent] \| None` | `None` | Sub-agents the human can re-run via `retry <agent>: <feedback>`. |
| `store` | `Store \| None` | `None` | Store the human can read via `store <key>`. |
| `input_fn` | `Callable[[str], str] \| None` | `None` | Sync prompt source. Default is `input()`; pass a custom one (e.g. `lazybridge.testing.scripted_inputs([...])`) for tests / non-terminal contexts. |
| `ainput_fn` | `Callable[[str], Awaitable[str]] \| None` | `None` | Async prompt source — use for event-loop-native / web-served REPLs so cancellation propagates. |
| `timeout` | `float \| None` | `None` | Per-prompt wait before falling back to `default`. |
| `default` | `str \| None` | `None` | Value used on timeout (enables unattended fallback). |
| `**agent_kwargs` | — | — | (`supervisor_agent` only) forwarded to `Agent(...)`. |

## REPL commands

At each turn the human sees the previous output and a prompt. Commands:

| Command | Effect |
|---|---|
| `continue [custom text]` | Exit the REPL — return the custom text, or the previous output if omitted. |
| `retry <agent>: <feedback>` | Re-run the named sub-agent with feedback; its new output replaces the buffer. |
| `store <key>` | Read and print `store[key]`. |
| `<tool>(<args>)` | Invoke one of the provided tools and print the result. |

Every command emits a **`HIL_DECISION`** session event (kind: `continue` /
`retry` / `store` / `tool` / `unknown` / `empty`), so the human's actions are
fully auditable in `session.events`.

## Example

```python
from lazybridge import Agent, Session
from lazybridge.ext.hil import SupervisorEngine

sess = Session()
researcher = Agent("claude-opus-4-7", name="researcher", session=sess)
writer     = Agent("claude-opus-4-7", name="writer",     session=sess)

supervisor = Agent(
    engine=SupervisorEngine(tools=[search_tool], agents=[researcher], store=sess.store),
    name="supervisor",
    session=sess,
)

# Drop the human between two automated agents.
pipeline = Agent.chain(researcher, supervisor, writer)
pipeline("AI safety report")
```

Drive it non-interactively (tests, or a web/HTTP front end) with `input_fn` /
`ainput_fn`:

```python
from lazybridge.testing import scripted_inputs

sup = Agent(engine=SupervisorEngine(input_fn=scripted_inputs(["continue"])), name="sup")
env = sup("task-x")          # runs the REPL with the scripted answer, no real stdin
```

## When to use it

- **Interactive debugging / steering** of a running pipeline.
- **High-stakes operations** where a human should be able to retry an agent,
  call a tool, or inspect state before continuing.
- **Demos and SLA-sensitive automation** with `timeout=` + `default=`.

## When NOT to use it

- **Approval / a single form is enough** → use [`HumanEngine`](human.md); it's
  lighter and purpose-built for one turn.
- **Fully unattended pipelines** → set `timeout=` + `default=`, or don't use HiL.
- **Web / HTTP-served** without an event loop story → wire `ainput_fn` rather
  than the default blocking `input()`.

## Security & safety

- **The REPL can call tools and read the store.** Only pass `tools=` / `store=`
  you're comfortable a human operator can reach; sub-agent guards still apply
  when a `retry` runs. See [Safety](../safety.md).
- **Audit trail.** Every action is a `HIL_DECISION` event — use it for review.
- **`default` on timeout becomes the decision.** Choose it conservatively for
  unattended runs.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Blocks forever on `input()` | Terminal REPL with no input source | Provide `input_fn` / `ainput_fn`, or run interactively; set `timeout=` for a fallback. |
| `retry <agent>` says unknown agent | Agent not in `agents=` | Add the agent to `agents=`; names are matched exactly. |
| `store <key>` returns nothing | No `store=` wired, or key absent | Pass `store=`; check the key exists. |
| Hangs under asyncio / web server | Using the sync `input_fn` path | Supply `ainput_fn` so the async REPL is used and cancellation propagates. |

## Pitfalls

- **The REPL is terminal-oriented by default.** For non-terminal contexts wire
  `ainput_fn` (or `input_fn` for scripted/sync sources).
- **`continue` with no text** returns the *previous* output unchanged.
- **One `HIL_DECISION` event per command** — rely on it for the audit trail, not
  on scraping stdout.

## See also

- [Human approval](human.md) — the single-turn engine for approvals / forms.
- [Human-in-the-loop overview](index.md) — choosing between the two engines.
- [Planners](../planners/index.md) — when an LLM (not a human) should dispatch.
