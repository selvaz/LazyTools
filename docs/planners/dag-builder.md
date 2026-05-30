# DAG builder (`orchestrator_agent`)

`orchestrator_agent` (alias `make_planner`) gives an LLM a list of sub-agents
plus five **builder tools** that compose a real `lazybridge.Plan` one step at a
time. Native parallelism, compile-time DAG validation, optional judge loop.

> Part of [Planners](index.md). See that page for the shared signature,
> parameter table, the `verify=` loop, and security model.

## Signature

```python
from lazybridge.ext.planners import make_planner   # == orchestrator_agent

make_planner(
    agents,                        # list[Agent], unique .name each — REQUIRED
    *,
    model="claude-opus-4-7",
    system=None,                   # default: generalist preamble + PLANNER_GUIDANCE
    name="planner",
    verbose=False,
    verify=None,
    max_verify=3,
) -> Agent
```

See [Planners → Parameters](index.md#parameters) for the full parameter table.

## The five builder tools

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

## Builder workflow

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

## `add_step` field reference

| Field | Meaning |
|---|---|
| `name` | Unique snake_case identifier within the plan. |
| `agent` | Sub-agent name (must exist in the registry). |
| `task_kind` | `"literal"` (use `task_text`) / `"from_prev"` (default; preceding step's output) / `"from_step"` (output of `task_step`) / `"from_parallel"` (alias of `from_step`; read ONE branch) / `"from_parallel_all"` (aggregate the WHOLE parallel band starting at `task_step` into one labelled-text join). |
| `task_text` | Required when `task_kind="literal"`. |
| `task_step` | Required when `task_kind` is `from_step` / `from_parallel` / `from_parallel_all`; must name an earlier step. |
| `context_kind` / `context_step` | Optional secondary input pulled into the step's context — use to combine TWO parallel branches in a join step (one as task, one as context). |
| `parallel` | `True` to run concurrently with adjacent `parallel=True` siblings. |

## Reading from a parallel band

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

!!! warning "Version note: from_parallel_all via the add_step builder tool"
    On lazybridge **0.9.0 / 0.9.1**, the `add_step` builder tool's schema lists
    only `literal` / `from_prev` / `from_step` / `from_parallel`, so an LLM
    driving the planner cannot *select* `from_parallel_all` through `add_step`
    (the runtime accepts it, but the value isn't in the tool's enum). On those
    versions it's reachable only through the typed `PlanSpec` / `StepSpec` path.
    A later lazybridge release adds `from_parallel_all` to the `add_step` schema
    so the value the guidance steers toward is selectable — see the LazyBridge
    CHANGELOG.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: agents list must not be empty` | Called the factory with `[]` | Pass at least one sub-agent. |
| `ValueError: agents must have unique names` | Two sub-agents share a `.name` | Give each sub-agent a distinct `name=`. |
| `add_step` returns `REJECTED: unknown agent …` | `agent` not in the registry | Use one of the names from `agents`; read the hint's available list. |
| `add_step` returns `REJECTED: … not yet defined` | Forward `from_step` / `from_parallel` reference | Add the referenced step first — steps must be in dependency order. |
| `run_plan` returns `PLAN_REJECTED: …` | DAG failed compile-time validation | Re-read the hint; fix the step names / sentinels and rebuild. |
| `run_plan` returns `PLAN_RUNTIME_ERROR: …` | A sub-agent raised at execution time | Inspect the sub-agent; the planner surfaces the error message verbatim. |

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
- **"Coarse steps, not micro-steps."** Prefer 2-6 meaningful units of work, not
  a step per sentence.
- **Canonical vs alias.** `orchestrator_agent` and `make_planner` are the *same*
  callable; pick one name and stay consistent.

## See also

- [Planners overview](index.md) — shared signature, parameters, verify loop.
- [Blackboard](blackboard.md) — the flat to-do-list variant.
- [`Plan`](https://core.lazybridge.com/guides/basic/plan/) — the static engine
  the builder materialises into.
- LazyBridge examples: `examples/patterns/plan_tool.py`,
  `agent_builds_plan.py`, `dynamic_planner.py`.
