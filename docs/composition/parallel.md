# Parallel (`Agent.parallel`)

`Agent.parallel(a, b, c)` runs agents **concurrently on the same task** and folds
their outputs into one result. Use it when you *know* you want N things to happen
at once. It returns a `ParallelAgent` — agent-shaped, so it composes like any
other agent.

> Part of [Composition sugar](index.md). `parallel` is a **classmethod on
> `Agent`** in the LazyBridge core — `from lazybridge import Agent`.

## Signature

```python
from lazybridge import Agent

Agent.parallel(
    *agents,                  # one or more Agent instances, run concurrently
    concurrency_limit=None,   # int | None — cap on simultaneous in-flight calls
    step_timeout=None,        # float | None — per-agent asyncio.wait_for deadline (s)
    **kwargs,                 # forwarded to ParallelAgent (name=, description=, session=…)
) -> ParallelAgent

env = multi(task)                                # ONE Envelope (labelled-text join)
branches = await multi.run_branches(task)        # list[Envelope] in input order
```

`ParallelAgent` is exported from `lazybridge` (`from lazybridge import ParallelAgent`).

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `*agents` | `Agent` | — | The agents to fan out; each runs on the **same** input task. |
| `concurrency_limit` | `int \| None` | `None` | Cap on simultaneous in-flight branch calls. `None` = unbounded (all at once). |
| `step_timeout` | `float \| None` | `None` | Per-branch deadline in seconds (`asyncio.wait_for`). `None` = no timeout. |
| `name` | `str` | `"parallel"` | Display name (via `**kwargs`). |
| `**kwargs` | — | — | Other constructor keywords: `description`, `session`, etc. |

## Synopsis

`parallel` returns a `ParallelAgent`. It is **agent-shaped** (duck-typed
`_is_lazy_agent=True`): callable, has `.run()`, `.as_tool()`, and composes inside
`Agent(tools=[...])`, a `Plan` step, or a [`chain`](chain.md).

```python
multi = Agent.parallel(eu_research, us_research, apac_research, name="regions")

env = multi("Summarise 2026 AI regulation")
print(env.text())
# [eu_research]
# <…>
#
# [us_research]
# <…>
#
# [apac_research]
# <…>
```

**Since 0.7.9, `__call__` returns ONE `Envelope`** whose payload is the
labelled-text join of every branch's output (with transitive cost rollup), so it
composes uniformly with other agents. For typed, per-branch access use the async
`run_branches`:

```python
import asyncio
branches = asyncio.run(multi.run_branches("Summarise 2026 AI regulation"))
# -> list[Envelope] in input order; branches[0] is eu_research's, etc.
```

## When to use it

- **Multi-source / multi-region fan-out** — query several backends or regions at
  once on the same input.
- **Ensemble / voting** — run N agents and compare or merge their answers.
- **Independent sub-tasks with a shared input** — throttle with
  `concurrency_limit`, bound latency with `step_timeout`.

## When NOT to use it

- **LLM-directed dispatch** (the model decides which/whether to run in parallel)
  → pass agents as `tools=[...]`; the engine emits parallel tool calls itself.
- **Conditional / routed flows or typed downstream consumption** → build a
  [`Plan`](https://core.lazybridge.com/guides/basic/plan/) with sentinels.
- **Sequential handoffs** → use [`Agent.chain`](chain.md).

## Security & safety

- **Each branch keeps its own guards.** Fan-out doesn't bypass a sub-agent's
  allow-lists or confirmation gates. See [Safety](../safety.md).
- **Bound the blast radius.** Use `concurrency_limit` to avoid hammering a
  downstream service and `step_timeout` so one slow branch can't hang the join.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Got one `Envelope`, expected a list | `__call__` folds to one `Envelope` (since 0.7.9) | Call `await multi.run_branches(task)` for the typed `list[Envelope]`. |
| Whole call fails when one branch errors | First branch error propagates as the wrapper's `.error` | Inspect per-branch via `run_branches`, or guard the flaky sub-agent. |
| Too many simultaneous calls / rate limits | No `concurrency_limit` | Pass `concurrency_limit=N`. |
| A branch hangs | No `step_timeout` | Pass `step_timeout=<seconds>`. |

## Pitfalls

- **`__call__` returns ONE `Envelope`, not `list[Envelope]`** (since 0.7.9). Read
  `env.text()` for the labelled-text join, or `await multi.run_branches(task)`
  for the typed list.
- **Same task to every branch.** `parallel` fans the *same* input out; for
  different per-branch inputs, use a `Plan` with a parallel band.
- **`run_branches` is async.** Call it with `await` / `asyncio.run(...)`.

## See also

- [Composition sugar overview](index.md) — parallel vs chain vs `Plan` vs tools.
- [Chain](chain.md) — the sequential counterpart.
- [DAG builder → Reading from a parallel band](../planners/dag-builder.md#reading-from-a-parallel-band)
  — parallel bands inside a planner-built `Plan`.
