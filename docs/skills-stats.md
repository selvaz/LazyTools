# Statistical agents — specialists, a supervisor, and a charted report

`lazytools.skills.stats_agents` and `lazytools.skills.stats_report` package
the statistical tools ([volatility/correlation/regression](statistical-analysis.md),
[regimes](regimes.md)) into reusable agents two different ways — pick the one
that fits the caller:

- **Agent-as-tool specialists + a supervisor** (`stats_agents`) — narrow
  single-purpose agents an outer orchestrator (or another agent's `tools=`)
  can call directly. No shared state, no report — just a clean division of
  labour with a cheap model per specialist.
- **A blackboard pipeline that ends in a charted HTML report**
  (`stats_report`) — reuses the [Analyst skills](skills-analyst.md)
  `Skill`/`Blackboard` machinery (including its `REGIME` skill, unchanged) to
  run volatility/correlation, regression and regime detection, then assemble
  a self-contained report with embedded charts. No new rendering or charting
  code: figures are `chart:`/`regimes:` [Report](report.md) artifact refs,
  resolved from market-data-hub and the regime depot exactly as they already
  are for the equity-report pipeline.

## Specialists + supervisor (agent-as-tool)

```python
from lazytools.skills.stats_agents import stats_supervisor

supervisor = stats_supervisor("deepseek-v4-flash")
result = supervisor(
    "Regress SPY weekly returns on TLT, GLD and QQQ for 2015-2024 and tell me "
    "which factor dominates."
)
print(result.text())
```

`stats_supervisor` builds three specialists, each on its **own** engine (a
shared engine would share one turn budget across the whole team), and gives
them to itself as tools — the supervisor sees each specialist's `name` +
`description` and routes by reading it, with no hand-written recipe:

| Specialist | Tools | Job |
|---|---|---|
| `volatility_correlation_analyst` | `StatisticalAnalysisTools`, filtered to `statistical_return_*` | annualised volatility, pairwise correlation, return outliers |
| `regime_analyst` | `RegimeTools` (`allow_write=` gates fitting) | hidden-Markov volatility regimes: detect, or interpret an existing fit |
| `regression_analyst` | The **same** `StatisticalAnalysisTools`, filtered to `statistical_regression_*` | OLS / Ridge / Lasso factor regressions |

`volatility_correlation_analyst` and `regression_analyst` build the same
underlying provider and filter its tool list two different ways — one
provider, two narrow specialists, no duplicated tool-loading code.

Build a specialist alone when you only need one job:

```python
from lazytools.skills.stats_agents import regression_analyst

analyst = regression_analyst("deepseek-v4-flash")
print(analyst("Regress SPY on TLT and QQQ weekly returns, 2015-2024 "
              "(OLS, robust_se='HAC').").text())
```

## Charted report (blackboard pipeline)

```python
from lazytools.skills.stats_report import stats_report_pipeline

pipeline = stats_report_pipeline(
    model="deepseek-v4-flash",
    symbols="SPY,TLT,GLD,QQQ", dependent="SPY", regressors="TLT,GLD,QQQ",
    start="2015-01-01", end="2024-12-31", frequency="W", regime_start="2010-01-01",
)
pipeline("Go.")
```

Four skills share one blackboard, run in dependency order:

| Skill | Reads | Writes |
|---|---|---|
| `vol_corr` | — | `vol_corr_summary` |
| `regression` | — | `regression_summary` |
| `regime` (from [Analyst skills](skills-analyst.md), reused unchanged) | `prices_ready` | `regime_result_key`, `regime_plot_key`, `regime_summary` |
| `stats_report` | `vol_corr_summary`, `regression_summary`, `regime_summary`, `regime_plot_key` | `report_path` |

`start`/`end`/`frequency` (and `regime_start`, since a regime fit
conventionally wants more history than the regression window) are
constructor arguments, not part of the call-time message: a `Step`'s task
text is fixed once the `Plan` is built, so the window has to be baked in at
construction rather than left to free text.

The `stats_report` skill's memo embeds a `chart:` figure (the instruments'
return series, for visual context on co-movement and volatility clustering)
and a `regimes:` figure (the regime-overlay plot `regime` generated) — both
resolved by [`ecosystem_resolvers`](report.md), not rendered by any new code
here. Saved in one step via `save_memo_html` so the embedded-image HTML never
round-trips through the model.

## Runnable examples

- [`examples/run_stats_agents_deepseek.py`](https://github.com/selvaz/LazyTools/blob/main/examples/run_stats_agents_deepseek.py)
  — live DeepSeek smoke test of all three specialists plus the supervisor
  delegating to two of them; asserts each expected tool was actually invoked.
- [`examples/run_stats_report_deepseek.py`](https://github.com/selvaz/LazyTools/blob/main/examples/run_stats_report_deepseek.py)
  — runs the full `stats_report_pipeline` live and asserts the resulting HTML
  has embedded images and populated tables.

## Why two patterns

Agent-as-tool is the right shape when a caller — a person, or another
agent's own `tools=` — just wants an answer to a question that spans one or
more of these domains, with no artifact to produce. The blackboard pipeline
is the right shape once the goal *is* an artifact (a saved report) built from
several specialists' handles: the same discipline [Analyst skills](skills-analyst.md)
already established (handles, not data, cross the blackboard; the contract is
enforced at the tool boundary) applies unchanged to a purely statistical
report — it did not need a second implementation of `Skill`/`Blackboard`,
only new skill *contracts*.
