# Planners

Hand an LLM a list of sub-agents and get back a single
`lazybridge.Agent` that **dynamically plans and dispatches** work to them.
Two factories, same input shape (`agents: list[Agent]`), different trade-offs:

- **[DAG builder](dag-builder.md)** — `orchestrator_agent` (alias
  `make_planner`). The LLM composes a real `lazybridge.Plan` one step at a time
  through five validated builder tools. Native parallelism, compile-time DAG
  validation, optional judge loop.
- **[Blackboard](blackboard.md)** — `blackboard_orchestrator_agent` (alias
  `make_blackboard_planner`). The LLM manages a flat to-do list through three
  blackboard tools. No DAG, no structural validation; easier to prompt, freer to
  re-plan.

> **Ships in `lazybridge` (core), not `lazytoolkit`.** Import from
> `lazybridge.ext.planners`. LazyBridge comes transitively with
> `pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@v0.3.2"`, or install it directly with `pip install lazybridge`.

!!! note "Naming: orchestrator vs planner"
    `orchestrator_agent` is the **canonical** name; it avoids the verbal
    collision with `lazybridge.Plan` (the *static* DAG engine). An orchestrator
    is an LLM agent that *dynamically* dispatches to sub-agents. The older names
    `make_planner` / `make_blackboard_planner` remain as **backward-compat
    aliases** bound to the exact same callables — existing code keeps working.

## Synopsis

Each sub-agent in `agents` becomes a **direct tool** on the returned planner —
so the planner can just *call one* when a single specialist is enough, with no
plan at all. On top of that, the planner gets a small set of **planning tools**:

- **DAG builder** → five builder tools that assemble a `Plan` incrementally,
  validating each step locally as it is added.
- **Blackboard** → three tools (`set_plan` / `get_plan` / `mark_done`) over a
  flat task list the LLM ticks off as it goes.

The integration sits entirely at the **agent + tool boundary**: the planner is
an ordinary `Agent` whose tools happen to be other agents plus a planning
toolkit. Nothing about how you *run* it differs from any other agent.

## When to use which

| You want… | Use |
|---|---|
| Parallel fan-out, structural validation, or a cost-aware verify gate | **[DAG builder](dag-builder.md)** (`orchestrator_agent`) |
| Exploratory work where the shape emerges as you go; freeform re-planning | **[Blackboard](blackboard.md)** (`blackboard_orchestrator_agent`) |
| A single specialist to handle the whole task | Neither — call the sub-agent directly (the planner will, too) |
| A *static*, code-defined pipeline you control fully | Not a planner — build a [`Plan`](https://core.lazybridge.com/guides/basic/plan/) directly |

## Signature

Both factories take the same input shape:

```python
from lazybridge.ext.planners import (
    orchestrator_agent,            # canonical — DAG builder
    make_planner,                  # alias of orchestrator_agent
    blackboard_orchestrator_agent, # canonical — flat to-do list
    make_blackboard_planner,       # alias of blackboard_orchestrator_agent
    # Building blocks / prompts:
    make_plan_builder_tools,       # the five builder tools, standalone
    PLANNER_GUIDANCE,              # system-prompt guidance for the DAG builder
    PLANNER_VERIFY_PROMPT,         # suggested judge prompt for verify=
    BLACKBOARD_PLANNER_GUIDANCE,   # system-prompt guidance for the blackboard
    PlanSpec, StepSpec,            # pydantic schemas the builder materialises
)

orchestrator_agent(               # == make_planner
    agents,                        # list[Agent], unique .name each — REQUIRED
    *,
    model="claude-opus-4-7",       # planner LLM
    system=None,                   # override prompt (default: generalist + PLANNER_GUIDANCE)
    name="planner",                # display name
    verbose=False,                 # print event traces
    verify=None,                   # optional judge Agent (approve/reject loop)
    max_verify=3,                  # max judge attempts when verify= is set
) -> Agent

blackboard_orchestrator_agent(    # == make_blackboard_planner
    agents, *, model="claude-opus-4-7", system=None,
    name="blackboard_planner", verbose=False, verify=None, max_verify=3,
) -> Agent
```

Both return a plain `Agent` — call it with the user task, exactly like any other
agent.

## Parameters

Shared by `orchestrator_agent` and `blackboard_orchestrator_agent`:

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `agents` | `list[Agent]` | — | Sub-agents the planner may dispatch to. Each must have a unique `.name`; the planner addresses them by that name. Empty list or duplicate names → `ValueError`. |
| `model` | `str` | `"claude-opus-4-7"` | Provider model id for the **planner** LLM (sub-agents keep their own engines). |
| `system` | `str \| None` | `None` | Override the planner's system prompt. Default prepends a generalist preamble to `PLANNER_GUIDANCE` (DAG builder) or uses `BLACKBOARD_PLANNER_GUIDANCE` (blackboard). |
| `name` | `str` | `"planner"` / `"blackboard_planner"` | Display name for the planner agent (shows up in sessions / traces). |
| `verbose` | `bool` | `False` | Print event traces to stdout. |
| `verify` | `Agent \| None` | `None` | Optional judge `Agent` that vets the final output via LazyBridge's built-in verify-with-retry loop. See [The verify loop](#the-verify-loop). |
| `max_verify` | `int` | `3` | Max judge attempts when `verify=` is set. |

## The verify loop

Pass a judge `Agent` as `verify=` to gate the planner's final answer. On each
attempt the judge sees the planner's output and replies `"approved"` or
`"rejected: <reason>"`; on rejection the planner retries (up to `max_verify`,
default 3) with the judge's feedback in context. Costs one extra LLM call per
attempt — use it where wrong answers are expensive. A ready-made judge prompt
ships as `PLANNER_VERIFY_PROMPT`.

```python
from lazybridge import Agent, LLMEngine
from lazybridge.ext.planners import make_planner, PLANNER_VERIFY_PROMPT

judge = Agent(engine=LLMEngine("claude-haiku-4-5", system=PLANNER_VERIFY_PROMPT),
              name="judge")

planner = make_planner([research, writer], verify=judge, max_verify=2)
```

Both planner flavours accept `verify=` / `max_verify=`.

## Security & safety

- **The planner can only call the agents you pass.** The sub-agent registry is
  fixed at construction; the builder rejects any `agent` not in it. Scope the
  blast radius by scoping `agents`.
- **Each sub-agent keeps its own guards.** Wrapping agents in a planner does not
  bypass their tool allow-lists or confirmation gates — those still fire when
  the sub-agent runs. See [Safety](../safety.md).
- **Bounded memory.** The DAG builder caps in-progress plans (oldest-evicted on
  overflow) and `run_plan` / `discard_plan` consume the plan, so a misbehaving
  planner can't leak unbounded plan state.
- **Extension surface.** Planners are a framework extension (`lazybridge.ext.*`),
  so the API may change between LazyBridge minor releases — pin a version and
  read the CHANGELOG before upgrading.

## See also

- [DAG builder](dag-builder.md) — the five builder tools, parallel bands, the
  full reference-grade walkthrough.
- [Blackboard](blackboard.md) — the flat to-do-list variant.
- [Core tools overview](../core-tools.md) — where the planners sit in the stack.
- [`Plan`](https://core.lazybridge.com/guides/basic/plan/) — the static,
  code-defined DAG engine the DAG builder materialises into.
- [Supervisor (REPL)](../hil/supervisor.md) — an HiL-flavoured alternative when a
  human (not an LLM) does the dispatching.
