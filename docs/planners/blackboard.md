# Blackboard (`blackboard_orchestrator_agent`)

`blackboard_orchestrator_agent` (alias `make_blackboard_planner`) gives an LLM a
list of sub-agents plus a **flat to-do list** instead of a DAG. No structural
validation, no native parallelism — but easier to prompt and freer to re-plan.
Use it for exploratory work where the shape emerges as you go.

> Part of [Planners](index.md). See that page for the shared signature,
> parameter table, the `verify=` loop, and security model.

## Signature

```python
from lazybridge.ext.planners import make_blackboard_planner  # == blackboard_orchestrator_agent

make_blackboard_planner(
    agents,                        # list[Agent], unique .name each — REQUIRED
    *,
    model="claude-opus-4-7",
    system=None,                   # default: BLACKBOARD_PLANNER_GUIDANCE
    name="blackboard_planner",
    verbose=False,
    verify=None,
    max_verify=3,
) -> Agent
```

See [Planners → Parameters](index.md#parameters) for the full parameter table.

## The three blackboard tools

Three tools over shared closure state:

| Tool | Effect |
|---|---|
| `set_plan(reasoning, tasks)` | Initialise or **reset** the plan — 3-6 coarse tasks in execution order. Empty `reasoning` or empty `tasks` is rejected. |
| `get_plan()` | Read current state with `[x]`/`[ ]` marks and recorded results, plus the next pending index. |
| `mark_done(task_index, result_summary)` | Tick a task and record a 1-3 sentence summary. Out-of-range index or empty summary is rejected. |

## Example

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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: agents list must not be empty` | Called the factory with `[]` | Pass at least one sub-agent. |
| `ValueError: agents must have unique names` | Two sub-agents share a `.name` | Give each sub-agent a distinct `name=`. |
| `mark_done` returns `REJECTED: no plan set` | `mark_done` before `set_plan` | Call `set_plan(...)` first. |
| `set_plan` returns `REJECTED` | Empty `reasoning` or empty `tasks` | Pass a non-empty reasoning string and a non-empty task list. |
| `mark_done` returns `REJECTED: … out of range` | `task_index` outside the plan | Use an index within the current task list (see `get_plan()`). |

## Pitfalls

- **Don't skip `mark_done`.** The loop relies on ticking tasks to know what's
  next; un-ticked tasks stall progress.
- **Coarse tasks, not micro-steps.** Aim for 3-6 self-contained items in
  execution order.
- **No native parallelism.** If you need fan-out + structural validation, use the
  [DAG builder](dag-builder.md) instead.
- **Canonical vs alias.** `blackboard_orchestrator_agent` and
  `make_blackboard_planner` are the *same* callable.

## See also

- [Planners overview](index.md) — shared signature, parameters, verify loop.
- [DAG builder](dag-builder.md) — the validated-DAG variant with parallelism.
- LazyBridge example: `examples/patterns/blackboard_planner.py`.
