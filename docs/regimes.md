# Regime detection

`lazytools.connectors.regimes.RegimeTools` exposes the HMM / Markov-switching
regime engines of
[`lazystats.regimes`](https://github.com/selvaz/LazyStats) as `regime_*`
LazyBridge tools. `lazystats.regimes` is designed around plain functions with
`Annotated[type, "description"]` parameters, so each tool is a thin
`Tool.wrap` — the math lives in lazystats, the LLM surface (and its output
caps) lives here.

Unlike the stateless `datahub_*` tools, regime fitting is **session state**:
loaded series, fitted results, and parameter sets live in an in-process or
SQLite-backed store shared across calls within one agent run — that is the
point of the `regime_store_*` / `regime_db_*` / `regime_params_*` tools.

## Tool surface

**Read (always exposed):** `regime_scan_state_counts`, `regime_get_current`,
`regime_get_changes`, `regime_get_summary`, `regime_compare_emission_models`,
`regime_compare_windows`, `regime_store_list`, `regime_store_load`,
`regime_params_list`, `regime_params_load`, and depot inspection
(`regime_db_list_series`, `regime_db_get_series_info`, `regime_db_list_results`,
`regime_db_get_result_summary`, `regime_db_list_plots`,
`regime_db_compare_results`, `regime_db_get_state_sequence`).

**Write (require `RegimeTools(allow_write=True)`):**
`regime_load_from_datahub`, `regime_fit`, `regime_fit_categorical`,
`regime_fit_window`, `regime_apply_params`, `regime_generate_plots`,
`regime_store_delete`, `regime_params_save`, and depot mutation
(`regime_db_export_plot`, `regime_db_delete_series`, `regime_db_delete_result`).

Output is hard-capped at the bridge (state sequences ≤ 500 timesteps, change
lists ≤ 200 entries, `truncated` flag reported) so a full posterior matrix can
never flood an LLM context.

## Data path

The **only** data loader on the agent surface is `regime_load_from_datahub` —
symbols/dates/frequency in, a bounded summary out, market-data-hub as the sole
source. The file-path loader in `lazystats.regimes` is a notebook-only
building block and is not reachable through this connector: an agent never
chooses what file to read.

## Install and add to an agent

```bash
pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git"
pip install "lazystats[regimes] @ git+https://github.com/selvaz/LazyStats.git"
pip install "market-data-hub @ git+https://github.com/selvaz/market-data-hub.git"
```

```python
from lazybridge import Agent
from lazytools.connectors.regimes import RegimeTools

# Read-only inspection of existing fits/depot:
agent = Agent("claude-opus-4-8", tools=[RegimeTools()])

# Full workflow — load from the hub, fit, summarize, plot:
agent = Agent("claude-opus-4-8", tools=[RegimeTools(allow_write=True)])
```

`lazystats` is imported lazily: the provider imports without it, and a missing
install raises an `ImportError` with the `[regimes]` install hint.
