# Planners

Hand an LLM a list of sub-agents and get back a single
`lazybridge.Agent` that **dynamically plans and dispatches** work to them.
Two factories, same input shape (`agents: list[Agent]`), different trade-offs:

- **`orchestrator_agent`** (alias `make_planner`) — a **DAG builder**. The LLM
  composes a real `lazybridge.Plan` one step at a time through five validated
  builder tools. Native parallelism, compile-time DAG validation, optional
  judge loop.
- **`blackboard_orchestrator_agent`** (alias `make_blackboard_planner`) — a
  **flat to-do list**. The LLM manages tasks through three blackboard tools. No
  DAG, no structural validation; easier to prompt, freer to re-plan.

> **Ships in `lazybridge` (core), not `lazytoolkit`.** Import from
> `lazybridge.ext.planners`. LazyBridge comes transitively with
> `pip install lazytoolkit`, or install it directly with `pip install lazybridge`.

!!! note "Naming: orchestrator vs planner"
    `orchestrator_agent` is the **canonical** name; it avoids the verbal
    collision with `lazybridge.Plan` (the *static* DAG engine). An orchestrator
    is an LLM agent that *dynamically* dispatches to sub-agents. The older names
    `make_planner` / `make_blackboard_planner` remain as **backward-compat
    aliases** bound to the exact same callables — existing code keeps working.

## Signature

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


# DAG builder — the LLM composes a Plan step by step.
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


# Blackboard — the LLM manages a flat todo list.
blackboard_orchestrator_agent(    # == make_blackboard_planner
    agents,                        # list[Agent], unique .name each — REQUIRED
    *,
    model="claude-opus-4-7",
    system=None,                   # override prompt (default: BLACKBOARD_PLANNER_GUIDANCE)
    name="blackboard_planner",
    verbose=False,
    verify=None,
    max_verify=3,
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
| Parallel fan-out, structural validation, or a cost-aware verify gate | **`orchestrator_agent`** (DAG builder) |
| Exploratory work where the shape emerges as you go; freeform re-planning | **`blackboard_orchestrator_agent`** |
| A single specialist to handle the whole task | Neither — call the sub-agent directly (the planner will, too) |
| A *static*, code-defined pipeline you control fully | Not a planner — build a [`Plan`](https://core.lazybridge.com/guides/basic/plan/) directly |

## DAG builder (`orchestrator_agent`)

The LLM composes a `Plan` one step at a time via five builder tools that **share
state via closure**:

| Tool | Effect |
|---|---|
| `create_plan(reasoning)` | Start a new empty plan. Returns a `plan_id`. `reasoning` is **required** — empty/boilerplate is rejected. |
| `add_step(plan_id, name, agent, …, parallel)` | Append one validated step. On rejection the plan is unchanged — fix the args and call again. |
| `inspect_plan(plan_id)` | Show the plan's current shape (useful between additions). |
| `run_plan(plan_id, task)` | Materialise + execute the plan; returns the final step's text. **Consumes** the plan. |
| `discard_plan(plan_id)` | Drop an in-progress plan without running it. |

Validation is **local**: each `add_step` rejects immediately with a pointed hint
(unknown agent, duplicate name, forward `from_step` reference, missing
`task_text`…), so the LLM corrects one step rather than re-emitting the whole DAG.

### Builder workflow

```python
from lazybridge import Agent, LLMEngine
from lazybridge.ext.planners import make_planner

research = Agent(engine=LLMEngine("claude-opus-4-7"), tools=[web_search],
                 name="research", description="Web lookups. No math.")
math     = Agent(engine=LLMEngine("claude-haiku-4-5"), tools=[add],
                 name="math",     description="Arithmetic only.")
writer   = Agent(engine=LLMEngine("claude-opus-4-7"),
                 name="writer",   description="Prose synthesis.")

planner = make_planner([research, math, writer])
result = planner("Research recent agent frameworks and write a one-paragraph summary.")
print(result.text())
```

Under the hood the planner runs a sequence like:

```text
create_plan(reasoning="Two-step pipeline: research gathers facts, writer drafts prose.")
add_step(pid, name="gather", agent="research")
add_step(pid, name="draft",  agent="writer")   # task_kind="from_prev" by default
run_plan(pid, task="<the user's task>")
```

### `add_step` field reference

| Field | Meaning |
|---|---|
| `name` | Unique snake_case identifier within the plan. |
| `agent` | Sub-agent name (must exist in the registry). |
| `task_kind` | `"literal"` (use `task_text`) / `"from_prev"` (default; preceding step's output) / `"from_step"` (output of `task_step`) / `"from_parallel"` (alias of `from_step`; read ONE branch) / `"from_parallel_all"` (aggregate the WHOLE parallel band starting at `task_step` into one labelled-text join). |
| `task_text` | Required when `task_kind="literal"`. |
| `task_step` | Required when `task_kind` is `from_step` / `from_parallel` / `from_parallel_all`; must name an earlier step. |
| `context_kind` / `context_step` | Optional secondary input pulled into the step's context — use to combine TWO parallel branches in a join step (one as task, one as context). |
| `parallel` | `True` to run concurrently with adjacent `parallel=True` siblings. |

### Reading from a parallel band

```python
# Two parallel lookups, then a writer that synthesises ALL branches.
create_plan(reasoning="Fan out N lookups, synthesise via from_parallel_all.")
add_step(pid, name="hc_apple",  agent="research", task_kind="literal",
         task_text="headcount of Apple in 2024",  parallel=True)
add_step(pid, name="hc_google", agent="research", task_kind="literal",
         task_text="headcount of Google in 2024", parallel=True)
add_step(pid, name="report",    agent="writer",
         task_kind="from_parallel_all", task_step="hc_apple")  # FIRST band member
run_plan(pid, task="Compare Apple and Google headcounts")
```

- `from_parallel` reads **one** specific branch (`task_step`).
- `from_parallel_all` aggregates the **whole** band — set `task_step` to the
  **first** `parallel=True` member; the join step receives a single
  labelled-text join of every branch's output.
- To combine exactly **two** branches, use `task_kind="from_parallel"` for A
  plus `context_kind="from_parallel"` + `context_step` for B.

## Blackboard (`blackboard_orchestrator_agent`)

A flat to-do list instead of a DAG. Three tools over shared closure state:

| Tool | Effect |
|---|---|
| `set_plan(reasoning, tasks)` | Initialise or **reset** the plan — 3-6 coarse tasks in execution order. Empty `reasoning` or empty `tasks` is rejected. |
| `get_plan()` | Read current state with `[x]`/`[ ]` marks and recorded results, plus the next pending index. |
| `mark_done(task_index, result_summary)` | Tick a task and record a 1-3 sentence summary. Out-of-range index or empty summary is rejected. |

```python
from lazybridge.ext.planners import make_blackboard_planner

planner = make_blackboard_planner([research, writer])
result = planner("Investigate the 2026 EU AI Act timeline and brief me.")
print(result.text())
```

The LLM loops: `set_plan(...)` → pick the next `[ ]` task → call the right
sub-agent → `mark_done(idx, summary)` → repeat → synthesise the final answer.
It can revise mid-flow by calling `set_plan` again.

!!! note "State resets per run"
    Closure state is reset on **every** `Agent.run` / `arun` invocation, so a
    blackboard planner reused across calls in one session never leaks the prior
    plan into the next run.

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

## Security & safety

- **The planner can only call the agents you pass.** The sub-agent registry is
  fixed at construction; `add_step` rejects any `agent` not in it. Scope the
  blast radius by scoping `agents`.
- **Each sub-agent keeps its own guards.** Wrapping agents in a planner does not
  bypass their tool allow-lists or confirmation gates — those still fire when
  the sub-agent runs. See [Safety](safety.md).
- **Bounded memory.** The DAG builder caps in-progress plans (oldest-evicted on
  overflow) and `run_plan` / `discard_plan` consume the plan, so a misbehaving
  planner can't leak unbounded plan state.
- **Extension surface.** Planners are a framework extension (`lazybridge.ext.*`),
  so the API may change between LazyBridge minor releases — pin a version and
  read the CHANGELOG before upgrading.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: agents list must not be empty` | Called the factory with `[]` | Pass at least one sub-agent. |
| `ValueError: agents must have unique names` | Two sub-agents share a `.name` | Give each sub-agent a distinct `name=`. |
| `add_step` returns `REJECTED: unknown agent …` | `agent` not in the registry | Use one of the names from `agents`; read the hint's available list. |
| `add_step` returns `REJECTED: … not yet defined` | Forward `from_step` / `from_parallel` reference | Add the referenced step first — steps must be in dependency order. |
| `run_plan` returns `PLAN_REJECTED: …` | DAG failed compile-time validation | Re-read the hint; fix the step names / sentinels and rebuild. |
| `run_plan` returns `PLAN_RUNTIME_ERROR: …` | A sub-agent raised at execution time | Inspect the sub-agent; the planner surfaces the error message verbatim. |
| Blackboard `mark_done` returns `REJECTED: no plan set` | `mark_done` before `set_plan` | Call `set_plan(...)` first. |

## Pitfalls

- **`from_parallel` reads ONE branch; `from_parallel_all` reads them all.** For
  three or more parallel legs feeding one synthesis step, use
  `from_parallel_all` with `task_step` set to the **first** band member.
- **Add steps in dependency order.** `add_step` rejects forward references —
  add the dependency step before the step that reads it.
- **Don't forget `run_plan`.** Building a plan without running it leaks it until
  the in-progress cap evicts it. If you change your mind, call `discard_plan`.
- **A `REJECTED: <hint>` is self-correctable.** The hint says exactly what's
  wrong; the planner should just call the tool again with the fix.
- **"Coarse steps, not micro-steps."** Both planners do best with 2-6 meaningful
  units of work, not a step per sentence.
- **Canonical vs alias.** `orchestrator_agent` and `make_planner` are the *same*
  callable; pick one name and stay consistent.

## See also

- [Core tools overview](core-tools.md) — where the planners sit in the stack.
- [`Plan`](https://core.lazybridge.com/guides/basic/plan/) — the static,
  code-defined DAG engine the DAG builder materialises into.
- [Supervisor pattern](https://core.lazybridge.com/guides/basic/supervisor/) —
  an HiL-flavoured alternative when a human (not an LLM) does the dispatching.
- LazyBridge examples: `examples/patterns/plan_tool.py`,
  `agent_builds_plan.py`, `dynamic_planner.py`, `blackboard_planner.py`.
