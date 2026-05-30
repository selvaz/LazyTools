# Core tools

The **connectors** in this site (Gmail, Telegram, MCP, the gateway, …) give an
agent reach into the outside world. **Core tools** are the complement: they give
an agent *structure over other agents* — orchestration, planning, and
delegation primitives that ship inside the [LazyBridge](https://github.com/selvaz/LazyBridge)
core itself, under `lazybridge.ext.*`.

!!! info "These live in `lazybridge`, not `lazytools`"
    Unlike the connectors (which live in the `lazytoolkit` package), the core
    tools are part of the **LazyBridge runtime** under `lazybridge.ext.*`. They
    are documented here, alongside the connectors, so you have one place to look
    when you ask *"what can I drop into `Agent(tools=[...])`?"* — but the code,
    the version, and the CHANGELOG are LazyBridge's. Install LazyBridge (it comes
    transitively with `pip install lazytoolkit`) and import from `lazybridge.ext.*`.

| Core tool | What it gives an agent | Import | Guide |
|---|---|---|---|
| **Planners** | Hand an LLM a list of sub-agents; get back one `Agent` that dynamically plans and dispatches work to them — a validated DAG builder *or* a flat blackboard to-do list. | `from lazybridge.ext.planners import …` | [Planners](planners.md) |

## Core vs ext vs connectors

LazyBridge draws a deliberate line between the stable runtime and the
extensions layered on top — see the upstream
[core-vs-ext guide](https://core.lazybridge.com/guides/core-vs-ext/). The short
version:

| Layer | Lives in | Holds |
|---|---|---|
| **Core** | `lazybridge/` | `Agent`, `LLMEngine`, `Plan`, `Step`, `Tool`, `Envelope`, `Memory`, `Store`, `Session`, sentinels, guards, providers |
| **Framework extensions** | `lazybridge/ext/*` | OpenTelemetry, HumanEngine / SupervisorEngine, evals, **planners**, visualizer |
| **Concrete tools** | `lazytoolkit` (this site) | connectors (Gmail, Telegram, MCP, gateway), document readers, skills |

The planners are a **framework extension**: they compose the core `Agent` and
`Plan` abstractions but are not part of the frozen core surface, so their API
may evolve between LazyBridge minor releases — pin a version and read the
LazyBridge CHANGELOG before upgrading.

Follow the [Planners](planners.md) guide for the full, reference-grade treatment.
