"""A charted statistical report over the Blackboard/Skill pipeline.

Composes existing pieces — no new rendering, charting or math code:

* :class:`~lazytools.skills.analyst.Skill` / :class:`~lazytools.skills.analyst.Blackboard`
  (the specialist/handle machinery already built for the equity-report pipeline).
* :data:`~lazytools.skills.analyst.REGIME` reused unchanged (load → fit → plot → publish).
* :class:`~lazytools.statistical_analysis.StatisticalAnalysisTools` (already
  hub-only) — one instance for volatility/correlation/outliers, filtered a
  second way for its ``statistical_regression_*`` (OLS/Ridge/Lasso) tools.
* :class:`~lazytools.report.ReportTools` + :func:`~lazytools.report.ecosystem_resolvers`
  for the ``chart:`` (on-demand hub series PNG) and ``regimes:`` (stored HMM
  plot PNG) figure schemes — both already implemented, neither reimplemented
  here.

Three new specialists (``vol_corr``, ``regression``, ``stats_report``) plus a
tiny deterministic pipeline are the only new code — Skill *contracts*
(system prompt + blackboard reads/writes), not new business logic.

    from lazytools.skills.stats_report import stats_report_pipeline

    pipeline = stats_report_pipeline(model="deepseek-v4-flash", symbols="SPY,TLT,GLD,QQQ")
    print(pipeline("Weekly, 2015-01-01 to 2024-12-31.").text())
"""

from __future__ import annotations

from typing import Any

from lazytools.skills.analyst import REGIME, AnalystConfig, Blackboard, Skill


# --------------------------------------------------------------------------- #
# Domain-tool builders
# --------------------------------------------------------------------------- #
def _vol_corr_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    return [StatisticalAnalysisTools()]


_REGRESSION_TOOL_NAMES = {
    "statistical_regression_ols",
    "statistical_regression_ridge",
    "statistical_regression_lasso",
}


def _regression_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    # Same provider as vol_corr, filtered to just its regression tools — no
    # separate provider, no separate loader; each tool reads its own series.
    return [t for t in StatisticalAnalysisTools().as_tools() if t.name in _REGRESSION_TOOL_NAMES]


def _stats_report_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.report import ReportFiles, ReportTools, ecosystem_resolvers

    files = ReportFiles(base_dir=cfg.out_dir)
    resolvers = ecosystem_resolvers(
        datahub_db_path=cfg.hub_db, regimes_db=cfg.regime_db, file_base_dir=cfg.out_dir
    )
    return [ReportTools(artifacts=resolvers, files=files), files]


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #
VOL_CORR = Skill(
    name="vol_corr",
    summary="Computes annualised volatility, pairwise correlation and outliers.",
    when_to_use="the report needs volatility, correlation or outlier statistics.",
    reads=(),
    writes=("vol_corr_summary",),
    system=(
        "You are the volatility & correlation specialist. For the requested "
        "instruments, window and frequency, call statistical_return_volatility, "
        "statistical_return_correlation and statistical_return_outliers (they read "
        "the series from market-data-hub themselves).\n"
        "Call publish(vol_corr_summary=<compact JSON: {instrument: annualized_volatility}, "
        "the pairwise correlation matrix, and total_outliers per instrument>)."
    ),
    build_tools=_vol_corr_tools,
)

REGRESSION = Skill(
    name="regression",
    summary="Runs OLS / Ridge / Lasso of one instrument's returns on the others.",
    when_to_use="the report needs a factor regression / explanatory-power breakdown.",
    reads=(),
    writes=("regression_summary",),
    system=(
        "You are the regression specialist. Call statistical_regression_ols "
        "(robust_se='HAC' for return data) for inference, and "
        "statistical_regression_ridge / statistical_regression_lasso to check "
        "shrinkage/variable selection. 'dependent' is exactly ONE instrument "
        "spec (e.g. 'SPY'); 'regressors' is a comma-separated list (e.g. "
        "'TLT,GLD,QQQ'), max 10. Each tool reads its own series from "
        "market-data-hub directly — never pass raw data.\n"
        "Call publish(regression_summary=<compact JSON: dependent, regressors, "
        "OLS coefficients with t/p-values, r_squared, and which Ridge/Lasso "
        "shrink or zero>)."
    ),
    build_tools=_regression_tools,
)

STATS_REPORT = Skill(
    name="stats_report",
    summary="Assembles a chart-embedded HTML report from the statistical findings.",
    when_to_use="all analyses are done and it is time to publish the statistical report.",
    reads=("vol_corr_summary", "regression_summary", "regime_summary", "regime_plot_key"),
    writes=("report_path",),
    system=(
        "You are the reporting specialist. Read vol_corr_summary, "
        "regression_summary, regime_summary and regime_plot_key from the "
        "blackboard (bb_get with those exact key names), then compose a memo and "
        "SAVE it in ONE step with save_memo_html(memo=<memo>, "
        "filename='stats_report.html'). Never render then save separately: an "
        "embedded-image HTML is too large to pass on and would be truncated.\n"
        "The memo shape is {title, as_of, sections:[{title, body, "
        "tables:[{columns, rows}], figures:[{ref, caption}]}], metadata}. "
        "Include:\n"
        "- an overview section with a figure ref "
        "'chart:symbols=<comma-separated tickers>&start=<window start>&"
        "end=<window end>&frequency=<frequency>&transform=log_return' — the "
        "instruments' return series, for visual context on co-movement and "
        "volatility clustering;\n"
        "- a volatility & correlation section: a table built from "
        "vol_corr_summary;\n"
        "- a regression section: a table of coefficients/significance built "
        "from regression_summary;\n"
        "- a regime section: a table of the regimes from regime_summary, plus a "
        "figure whose ref is exactly 'regimes:' followed by the regime_plot_key "
        "handle value (never a file: ref).\n"
        "All table cells must be strings. Finally call "
        "publish(report_path=<the path returned by save_memo_html>)."
    ),
    build_tools=_stats_report_tools,
)

STATS_REPORT_SKILLS: tuple[Skill, ...] = (VOL_CORR, REGRESSION, REGIME, STATS_REPORT)


# --------------------------------------------------------------------------- #
# Building + orchestrating
# --------------------------------------------------------------------------- #
def build_stats_report_specialists(
    *,
    model: str,
    cfg: AnalystConfig | None = None,
    store: Any = None,
    session: Any = None,
    max_turns: int = 20,
) -> dict[str, Any]:
    """Build the four specialists sharing one blackboard (see ``build_specialists``)."""
    from lazybridge import LLMEngine, Store

    cfg = cfg or AnalystConfig()
    store = store if store is not None else Store()
    blackboard = Blackboard(store)

    import lazystats.regimes.db as _rdb

    _rdb.init_regime_db(cfg.regime_db)

    system = "Follow your skill's instructions exactly; be terse and call tools one at a time."
    return {
        s.name: s.agent(
            LLMEngine(model, system=f"{system}\n\n{s.system}", max_turns=max_turns),
            cfg, blackboard, session=session,
        )
        for s in STATS_REPORT_SKILLS
    }


def stats_report_pipeline(
    *,
    model: str,
    symbols: str,
    dependent: str = "",
    regressors: str = "",
    start: str = "",
    end: str = "",
    frequency: str = "D",
    regime_start: str = "",
    cfg: AnalystConfig | None = None,
    session: Any = None,
    name: str = "stats_report_pipeline",
) -> Any:
    """Deterministic pipeline: vol_corr + regression run, then regime, then the report.

    ``symbols`` is the comma-separated universe (e.g. ``'SPY,TLT,GLD,QQQ'``);
    ``dependent``/``regressors`` default to the first symbol vs. the rest.
    ``start``/``end``/``frequency`` bound the vol/corr/regression window and the
    report's overview chart — baked directly into each step's task text (a
    ``Step.task`` is static once built, so the window/frequency must be fixed
    here rather than left to a free-text call-time message). ``regime_start``
    (default: ``start``, or the full history if ``start`` is also empty) bounds
    only the regime fit, which conventionally wants more history than the
    vol/corr/regression window.
    """
    from lazybridge import Agent, Plan, Step

    tickers = [s.strip() for s in symbols.split(",") if s.strip()]
    dependent = dependent or tickers[0]
    regressors = regressors or ",".join(tickers[1:])
    window = f"from {start or 'the earliest available date'} to {end or 'the latest available date'}"
    regime_from = regime_start or start or "the earliest available date"

    specialists = build_stats_report_specialists(model=model, cfg=cfg, session=session)
    order = ["vol_corr", "regression", "regime", "stats_report"]
    tasks = {
        "vol_corr": (
            f"Compute annualised volatility, correlation and outliers for {symbols}, "
            f"frequency='{frequency}', {window}."
        ),
        "regression": (
            f"Regress {dependent} on {regressors} (OLS with HAC, plus Ridge and Lasso), "
            f"frequency='{frequency}', {window}."
        ),
        "regime": (
            f"Fit a 3-regime volatility HMM for {dependent} at frequency='{frequency}' "
            f"starting {regime_from}, on the full available history from that start, and plot it."
        ),
        "stats_report": (
            f"Assemble and save the statistical HTML report for {symbols}. For the overview "
            f"chart use frequency='{frequency}', {window}."
        ),
    }
    steps = [Step(n, task=tasks[n]) for n in order]
    return Agent(
        name=name,
        engine=Plan(*steps),
        tools=[specialists[n] for n in order],
        description="Deterministic pipeline: volatility/correlation + regression + regime -> charted HTML report.",
        session=session,
    )


__all__ = [
    "VOL_CORR",
    "REGRESSION",
    "STATS_REPORT",
    "STATS_REPORT_SKILLS",
    "build_stats_report_specialists",
    "stats_report_pipeline",
]
