# Chain (`Agent.chain`)

`Agent.chain(a, b, c)` runs agents **sequentially**: the output of each becomes
the input to the next. It's one-liner sugar for a linear `lazybridge.Plan`, and
it returns a real `Agent`.

> Part of [Composition sugar](index.md). `chain` is a **classmethod on `Agent`**
> in the LazyBridge core — `from lazybridge import Agent`.

## Signature

```python
from lazybridge import Agent

Agent.chain(
    *agents,          # one or more Agent instances, run in order
    **kwargs,         # forwarded to the Agent(...) constructor
                      #   e.g. name="pipeline", session=..., tools=..., verify=...
) -> Agent            # an Agent whose engine is a linear Plan
```

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `*agents` | `Agent` | — | The agents to run in order; each step's output feeds the next. Each should have a distinct `.name` (it becomes the step name). |
| `name` | `str` | `"chain"` | Display name for the resulting pipeline agent (via `**kwargs`). |
| `**kwargs` | — | — | Any other `Agent(...)` keyword: `session`, `description`, `verify`, `max_verify`, `tools`, etc. Applied to the chain agent as a whole. |

## Synopsis

`chain` builds a `Plan` with one `Step` per agent and wraps it in an `Agent`:

```python
# Sugar
pipeline = Agent.chain(researcher, editor, writer, name="pipeline")

# …is equivalent to the explicit Plan:
from lazybridge import Plan, Step
pipeline = Agent(
    engine=Plan(
        Step(target=researcher, name=researcher.name),
        Step(target=editor,     name=editor.name),
        Step(target=writer,     name=writer.name),
    ),
    name="pipeline",
)
```

Calling it runs the steps in order and returns **one `Envelope`** — the final
step's output:

```python
report = pipeline("Brief me on the state of fusion energy in 2026.")
print(report.text())
```

Because the result is a normal `Agent`, the whole chain shares one session,
memory, and guard boundary, and can itself be a step in a larger `Plan` or an
entry in another agent's `tools=[...]`.

## When to use it

- **Linear multi-agent pipelines** — research → edit → write, where each stage
  consumes the previous stage's output.
- **Quick "crew"-style handoffs** — a CrewAI-style sequential crew in one line.
- **A single session / memory / guard boundary** around a fixed sequence.

## When NOT to use it

- **Typed hand-offs, conditional routing, or crash-resume** → build an explicit
  [`Plan`](https://core.lazybridge.com/guides/basic/plan/); `chain` is the
  no-frills linear case.
- **Fan-out (N agents on the same task)** → use [`Agent.parallel`](parallel.md).
- **LLM-directed dispatch** (the model decides what to call) → pass agents as
  `tools=[...]`, or use a [planner](../planners/index.md).

## Security & safety

- **Each sub-agent keeps its own guards.** Chaining does not bypass a sub-agent's
  tool allow-lists or confirmation gates — they fire when that agent runs. See
  [Safety](../safety.md).
- **One boundary for the pipeline.** `**kwargs` like `session=` / `verify=` apply
  to the chain agent as a whole; per-agent config stays on each sub-agent.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Step names collide / overwrite | Two sub-agents share a `.name` | Give each sub-agent a distinct `name=` before chaining. |
| Downstream step sees the wrong input | Misunderstood data flow | Each step receives the **previous** step's output; reorder agents or insert an adapter agent. |
| Need a branch's typed output, not text | `chain` returns one folded `Envelope` | Use an explicit `Plan` with sentinels, or [`parallel`](parallel.md) + `run_branches`. |

## Pitfalls

- **`chain` is sugar for a *linear* `Plan`.** The moment you can see a router, a
  parallel band, or a typed hand-off coming, reach for the explicit `Plan`.
- **Output is the last step's `Envelope`.** Intermediate outputs aren't returned;
  if you need them, use a `Plan` (and a `Store`) or inspect the session events.
- **Distinct names matter.** The agent `.name` becomes the step name.

## See also

- [Composition sugar overview](index.md) — chain vs parallel vs `Plan` vs tools.
- [Parallel](parallel.md) — the concurrent counterpart.
- [`Plan`](https://core.lazybridge.com/guides/basic/plan/) — the canonical form
  `chain` is sugar over.
- LazyBridge example: `examples/crewai/02_research_and_report.py`.
