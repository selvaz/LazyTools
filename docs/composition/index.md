# Composition sugar

`chain` and `parallel` are **pre-packaged pipelines** — one-liner sugar for
composition shapes you'd otherwise spell out as a `lazybridge.Plan` or a manual
fan-out. Conceptually they're the *scripted* cousins of the [Planners](../planners/index.md):
**you** decide the shape up front, instead of letting an LLM build it.

- **[Chain](chain.md)** — `Agent.chain(a, b, c)` runs agents **sequentially**:
  each agent's output becomes the next one's input. Returns a real `Agent`
  (engine = a linear `Plan`).
- **[Parallel](parallel.md)** — `Agent.parallel(a, b, c)` runs agents
  **concurrently** on the same task and folds the results. Returns a
  `ParallelAgent` (agent-shaped — callable, composable, usable as a tool).

> **Ships in `lazybridge` (core).** Unlike the planners and HiL factories (which
> live under `lazybridge.ext.*`), `chain` and `parallel` are **classmethods on
> `Agent`** in the core runtime: `from lazybridge import Agent` →
> `Agent.chain(...)` / `Agent.parallel(...)`. LazyBridge comes transitively with
> `pip install lazytoolkit`.

## When to use which

The deciding question is **who decides the flow** and **what shape** it is:

| Who decides the flow? | Use |
|---|---|
| You — linear, fixed handoffs | **[`Agent.chain(a, b, c)`](chain.md)** |
| You — deterministic fan-out on one task | **[`Agent.parallel(a, b, c)`](parallel.md)** |
| You — a declared DAG with types / routing / resume | [`Plan(Step(…), …)`](https://core.lazybridge.com/guides/basic/plan/) |
| The LLM — which to call, when, and whether in parallel | `Agent(tools=[a, b, c])` (or a [planner](../planners/index.md)) |

`chain` is sugar for a **linear `Plan`**; reach for an explicit `Plan` the moment
you can see a router or a typed hand-off coming. `parallel` is scripted fan-out;
if you want the *model* to decide whether to call agents in parallel, pass them
as `tools=[...]` instead — the engine emits parallel tool calls automatically
when the model requests them.

## Both behave as agents

Whatever you build composes uniformly: a chain is an `Agent`, a parallel is
agent-shaped, and either can be:

- called with a task (`pipe("…")`) → returns one `Envelope`;
- nested inside another agent's `tools=[...]` (via `.as_tool()`);
- dropped into a `Plan` step or another `chain` / `parallel`.

## See also

- [Chain](chain.md) — sequential pipeline reference.
- [Parallel](parallel.md) — fan-out reference.
- [Planners](../planners/index.md) — when an LLM (not you) should build the shape.
- [Core tools overview](../core-tools.md) — where these sit in the stack.
