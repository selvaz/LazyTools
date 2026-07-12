# Analyst skills — composing agents from skills

`lazytools.skills.analyst` is a worked example of the idea behind LazyTools:
**a tool provider should give an agent the *skill* to use its tools, not just
the tools.** It turns the connector tools into five specialist agents and shows
three ways to orchestrate them — a deterministic Plan, a blackboard planner, and
an adaptive replan loop — all over one shared blackboard.

## The idea

A **skill** is a specialist [`Agent`](https://github.com/selvaz/LazyBridge) —
domain tools plus a tailored system prompt — whose **description is a
contract**: what it does, when to use it, and which short *handles* it reads
from and writes to a shared blackboard. Because an agent's `name` + `description`
become the tool an orchestrator sees, an orchestrator built from skills *knows
what each skill does* — no hand-written recipe in a prompt.

Two rules make this compose:

- **Handles, not data, cross the blackboard.** Each specialist persists heavy
  artifacts where they belong — prices/facts in the market-data-hub DuckDB,
  fitted regimes and plots in the LazyStats depot, the report on disk — and puts
  only short *handles* (a `result_key`, a `plot_key`, a path, a compact JSON
  summary) on a shared `lazybridge.Store`. This is the same discipline that
  keeps big payloads out of the LLM's token stream.
- **The contract is enforced at the tool boundary.** Each specialist reads its
  declared inputs through a *scoped* `bb_get` and produces its outputs through a
  single typed `publish` tool whose parameters are exactly its declared handles.
  A small model can't invent or forget a handle name.

The five skills and their handle graph:

| Skill | Reads | Writes |
|---|---|---|
| `market_data` | — | `prices_ready` |
| `financials` | — | `balance_sheet` |
| `stats` | `prices_ready` | `vola_outlier` |
| `regime` | `prices_ready` | `regime_result_key`, `regime_plot_key`, `regime_summary` |
| `report` | `balance_sheet`, `vola_outlier`, `regime_summary`, `regime_plot_key` | `report_path` |

## One set of skills, three orchestrators

```python
from lazybridge import Session, Store
from lazytools.skills import AnalystConfig, build_specialists, plan_orchestrator

store, session = Store(), Session(db="analyst_events.db", console=True)
specialists = build_specialists(
    model="deepseek-v4-flash",          # each specialist gets its OWN engine
    cfg=AnalystConfig(regime_db="regimes.db", out_dir="reports"),
    store=store, session=session,
)

# pick ONE orchestrator over the SAME specialists:
orchestrator = plan_orchestrator(specialists, ticker="AMZN", session=session)
orchestrator("Produce a quantitative report on AMZN.")
print(store.read("report_path"))
```

- **`plan_orchestrator`** — deterministic pipeline in dependency order.
  Reproducible and checkpointable; the most reliable on a cheap model.
- **`blackboard_orchestrator(model=…)`** — a planner keeps a flat to-do list and
  picks the next ready specialist by reading its description.
- **`replan_orchestrator(planner_model=…)`** — plan → execute → observe →
  replan; adapts on failure (e.g. registers a new ticker after an ingestion
  error). Benefits from a stronger tier for the planner.

`build_specialists` gives **each specialist its own engine** on purpose: a single
shared engine instance would share one turn budget across the whole pipeline and
starve the last specialist.

## Runnable example + visualization

- [`examples/analyst_report.py`](../examples/analyst_report.py) — builds the
  specialists and runs any orchestrator (`LB_ORCH=plan|blackboard|replan`),
  producing a self-contained HTML report with an embedded regime chart.
- [`examples/analyst_viz.py`](../examples/analyst_viz.py) — renders the flow as a
  self-contained HTML diagram: the specialists and the blackboard from the skill
  contracts, with a real run's order, timing and produced handles overlaid from
  the shared `Session` event log.

## Why this matters

Given a pile of tools, either the model rediscovers the workflow every run
(unreliable) or a human writes the recipe every run (not autonomous, not
reusable). A **skill packaged with the tools** — a specialist agent whose
description is a contract, enforced at the tool boundary — is reusable and
reliable, and lets the agent be genuinely autonomous *within a scaffold the
tools provide*.
"""
