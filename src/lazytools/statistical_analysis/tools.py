"""Volatility, correlation and return-outlier tools for LazyBridge agents.

The math itself lives in ``lazystats.core.returns`` (plan v3.1 Fase 6): this
module is pure wrapping — LLM-facing signatures, output caps, serialization —
so the two repos never drift into two different implementations of the same
formulas. ``lazystats`` is imported lazily with a clear install hint.
"""

from __future__ import annotations

import json
from typing import Any

from lazybridge import Tool

from lazytools.statistical_analysis.backend import (
    MarketDataHubStatisticsBackend,
    ReturnDataset,
    StatisticalDataBackend,
)

_TOOL_VERSION = "lazytools.statistical_analysis.v1"
_PERIODS_PER_YEAR = {"D": 252, "W": 52, "M": 12, "Q": 4}
_DEFAULT_OUTLIER_RESULTS = 100
_MAX_OUTLIER_RESULTS = 250
_MAX_REGRESSORS = 10
# transforms whose per-period standard deviation can meaningfully be
# annualized with sqrt(periods_per_year)
_RETURN_TRANSFORMS = {"log_return", "pct_change"}

_SPEC_SYNTAX = (
    "each instrument is '<id>[|<transform>]' with transform in "
    "level|log_return|pct_change|diff (defaults: ticker log_return, "
    "factor level, macro diff), e.g. 'ticker:SPY, ticker:AAPL|level, "
    "macro:FEDFUNDS|diff, factor:FF5_daily/Mkt-RF'"
)


def _lazystats():
    try:
        from lazystats import core as _core
        from lazystats import models as _models
    except ImportError as exc:  # pragma: no cover - exercised without lazystats
        raise ImportError(
            "lazytools.statistical_analysis requires lazystats: pip install "
            "'lazystats @ git+https://github.com/selvaz/LazyStats.git'"
        ) from exc
    return _core, _models


def _lazyregression():
    try:
        from lazystats import regression as _regression
    except ImportError as exc:  # pragma: no cover - exercised without lazystats
        raise ImportError(
            "the regression tools require lazystats with its regression extra: pip install "
            "'lazystats[regression] @ git+https://github.com/selvaz/LazyStats.git'"
        ) from exc
    return _regression


def _as_lazystats_dataset(dataset: ReturnDataset) -> Any:
    _, models = _lazystats()
    return models.ReturnDataset(
        instruments=dataset.instruments, rows=dataset.rows, metadata=dataset.metadata
    )


class StatisticalAnalysisTools:
    """A LazyBridge ``ToolProvider`` for return statistics from market-data-hub.

    Every calculation reads its price history from the central
    ``market-data-hub`` DuckDB warehouse. ``instruments`` accepts canonical
    lazydatacore IDs (``ticker:SPY,ticker:TLT``); bare ticker symbols are also
    accepted as a convenience and are canonicalised by the hub backend.
    """

    _is_lazy_tool_provider = True

    def __init__(self, backend: StatisticalDataBackend | None = None) -> None:
        self._backend = backend

    def _resolve(self) -> StatisticalDataBackend:
        if self._backend is None:
            self._backend = MarketDataHubStatisticsBackend()
        return self._backend

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._return_volatility,
                name="statistical_return_volatility",
                description=(
                    "Calculate per-instrument standard deviation (and, for return series, "
                    "annualised volatility) from market-data-hub only. Args: instruments "
                    f"(comma-separated; {_SPEC_SYNTAX}), start/end (YYYY-MM-DD, optional), "
                    "frequency (D|W|M|Q, default D). Returns an AnalysisResult JSON."
                ),
            ),
            Tool.wrap(
                self._return_correlation,
                name="statistical_return_correlation",
                description=(
                    "Calculate pairwise Pearson correlations between series read only from "
                    f"market-data-hub. Args: instruments (comma-separated; {_SPEC_SYNTAX}), "
                    "start/end (YYYY-MM-DD, optional), frequency (D|W|M|Q, default D), "
                    "min_periods (default 2). Returns an AnalysisResult JSON with correlation "
                    "and pairwise-observation matrices."
                ),
            ),
            Tool.wrap(
                self._return_outliers,
                name="statistical_return_outliers",
                description=(
                    "Find outlier observations in series read only from market-data-hub. "
                    "For each instrument, z-score = (value - selected-period mean) / selected-"
                    "period sample standard deviation; flags abs(z-score) >= threshold. Args: "
                    f"instruments (comma-separated; {_SPEC_SYNTAX}), start/end (YYYY-MM-DD, "
                    "optional), frequency (D|W|M|Q, default D), threshold (absolute z-score, "
                    "default 2), max_results (default 100, hard cap 250). "
                    "Returns an AnalysisResult JSON including dates, values and z-scores."
                ),
            ),
            Tool.wrap(
                self._regression_ols,
                name="statistical_regression_ols",
                description=(
                    "Fit an OLS regression (univariate or multivariate, statsmodels) between "
                    "series read only from market-data-hub — never pass raw data. Args: "
                    f"dependent (one instrument spec; {_SPEC_SYNTAX}), regressors (comma-"
                    f"separated specs, max {_MAX_REGRESSORS}), start/end (YYYY-MM-DD, "
                    "optional), frequency (D|W|M|Q, default D), robust_se (nonrobust|HC0|HC1|"
                    "HC2|HC3|HAC, default nonrobust; HAC = Newey-West), hac_lags (HAC only, "
                    "0 = automatic), standardize (z-score all series first, default false). "
                    "Returns an AnalysisResult JSON with coefficients (std errors, t-stats, "
                    "p-values, confidence intervals), R², F, AIC/BIC, Durbin-Watson and "
                    "residual diagnostics — never the raw series or residuals."
                ),
            ),
            Tool.wrap(
                self._regression_ridge,
                name="statistical_regression_ridge",
                description=(
                    "Fit a Ridge regression (scikit-learn, standardized regressors) between "
                    "series read only from market-data-hub. Args: dependent (one instrument "
                    f"spec; {_SPEC_SYNTAX}), regressors (comma-separated specs, max "
                    f"{_MAX_REGRESSORS}), start/end (YYYY-MM-DD, optional), frequency "
                    "(D|W|M|Q, default D), alpha (empty = automatic cross-validated "
                    "selection), cv_folds (default 5). Returns an AnalysisResult JSON with "
                    "the chosen alpha, coefficients in original and standardized units and "
                    "R² — never the raw series."
                ),
            ),
            Tool.wrap(
                self._regression_lasso,
                name="statistical_regression_lasso",
                description=(
                    "Fit a Lasso regression (scikit-learn, standardized regressors, variable "
                    "selection) between series read only from market-data-hub. Args: "
                    f"dependent (one instrument spec; {_SPEC_SYNTAX}), regressors (comma-"
                    f"separated specs, max {_MAX_REGRESSORS}), start/end (YYYY-MM-DD, "
                    "optional), frequency (D|W|M|Q, default D), alpha (empty = automatic "
                    "cross-validated selection), cv_folds (default 5). Returns an "
                    "AnalysisResult JSON with the chosen alpha, coefficients, the surviving "
                    "(non-zero) regressors and R² — never the raw series."
                ),
            ),
        ]

    def _return_volatility(
        self,
        instruments: str,
        start: str = "",
        end: str = "",
        frequency: str = "D",
    ) -> str:
        """Per-instrument sample standard deviation, annualised for return series.

        Reads every series exclusively from market-data-hub inside this tool —
        never pass raw price or return data as an argument. Annualised
        volatility (period std * sqrt(periods_per_year)) is only meaningful
        for a return-flavoured transform; for level/diff it is reported as
        null instead of a misleading number.

        Args:
            instruments: comma-separated '<id>[|transform]' specs, see tool description for syntax.
            start: inclusive start date YYYY-MM-DD, empty for the earliest available date.
            end: inclusive end date YYYY-MM-DD, empty for the most recent available date.
            frequency: resample frequency, one of D, W, M, Q; defaults to D (native daily grid).

        Returns:
            AnalysisResult JSON. payload.volatility maps each instrument to
            observations, mean/period statistics and annualized_volatility
            (null for non-return transforms). payload.data carries bounded
            provenance only (source, per-series transform, date range) —
            never the raw observations.
        """
        core, _ = _lazystats()
        dataset = self._load(instruments, start=start, end=end, frequency=frequency)
        payload = core.return_volatility(_as_lazystats_dataset(dataset), frequency=frequency)
        _drop_meaningless_annualization(payload, dataset)
        payload["data"] = _data_metadata(dataset)
        return _analysis_json(
            kind="report",
            produced_by="lazytools.statistical_analysis.return_volatility",
            instruments=dataset.instruments,
            payload=payload,
        )

    def _return_correlation(
        self,
        instruments: str,
        start: str = "",
        end: str = "",
        frequency: str = "D",
        min_periods: int = 2,
    ) -> str:
        """Pairwise Pearson correlation between series read only from market-data-hub.

        Each coefficient uses only the dates where both instruments have an
        observation; the accompanying pairwise_observations matrix reports
        that shared sample size so a thin overlap is visible, not hidden.

        Args:
            instruments: comma-separated '<id>[|transform]' specs, see tool description for syntax.
            start: inclusive start date YYYY-MM-DD, empty for the earliest available date.
            end: inclusive end date YYYY-MM-DD, empty for the most recent available date.
            frequency: resample frequency, one of D, W, M, Q; defaults to D (native daily grid).
            min_periods: minimum shared observations required for a pair, else null; default 2.

        Returns:
            AnalysisResult JSON with payload.correlation (instrument x
            instrument Pearson matrix) and payload.pairwise_observations
            (shared-sample-size matrix) — never the raw series.
        """
        core, _ = _lazystats()
        dataset = self._load(instruments, start=start, end=end, frequency=frequency)
        payload = core.return_correlation(
            _as_lazystats_dataset(dataset), frequency=frequency, min_periods=min_periods
        )
        payload["data"] = _data_metadata(dataset)
        return _analysis_json(
            kind="report",
            produced_by="lazytools.statistical_analysis.return_correlation",
            instruments=dataset.instruments,
            payload=payload,
        )

    def _return_outliers(
        self,
        instruments: str,
        start: str = "",
        end: str = "",
        frequency: str = "D",
        threshold: float = 2.0,
        max_results: int = _DEFAULT_OUTLIER_RESULTS,
    ) -> str:
        """Dates where a series' z-score exceeds a threshold, read only from market-data-hub.

        z-score = (value - selected-period mean) / selected-period sample
        standard deviation, computed once per instrument over the whole
        requested window (not a rolling window). An observation is flagged
        when abs(z-score) >= threshold.

        Args:
            instruments: comma-separated '<id>[|transform]' specs, see tool description for syntax.
            start: inclusive start date YYYY-MM-DD, empty for the earliest available date.
            end: inclusive end date YYYY-MM-DD, empty for the most recent available date.
            frequency: resample frequency, one of D, W, M, Q; defaults to D (native daily grid).
            threshold: absolute z-score cutoff to flag an observation; default 2.0.
            max_results: cap on returned outliers, default 100, hard cap 250 regardless of input.

        Returns:
            AnalysisResult JSON with payload.total_outliers (true count) and
            payload.outliers (date, instrument, value, z_score, direction),
            truncated to max_results with payload.truncated set when capped.
        """
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        core, _ = _lazystats()
        dataset = self._load(instruments, start=start, end=end, frequency=frequency)
        # lazystats.core returns EVERY outlier; the LLM-context cap is applied
        # here, at the bridge, not in the pure library.
        result = core.return_outliers(
            _as_lazystats_dataset(dataset), frequency=frequency, threshold=threshold
        )
        outliers = result["outliers"]
        total_outliers = result["total_outliers"]
        returned = outliers[: min(max_results, _MAX_OUTLIER_RESULTS)]
        payload = {
            **result,
            "returned_outliers": len(returned),
            "truncated": total_outliers > len(returned),
            "outliers": returned,
            "data": _data_metadata(dataset),
        }
        return _analysis_json(
            kind="signal",
            produced_by="lazytools.statistical_analysis.return_outliers",
            instruments=dataset.instruments,
            payload=payload,
        )

    def _regression_ols(
        self,
        dependent: str,
        regressors: str,
        start: str = "",
        end: str = "",
        frequency: str = "D",
        robust_se: str = "nonrobust",
        hac_lags: int = 0,
        standardize: bool = False,
    ) -> str:
        """Univariate or multivariate OLS (statsmodels) between series from market-data-hub.

        The dependent variable and every regressor are read exclusively from
        market-data-hub inside this tool — never pass raw series as an
        argument. Univariate regression is simply the one-regressor case,
        no separate tool exists for it. Rows where any series is missing
        after alignment are dropped (complete-case); n_dropped reports how
        many.

        Args:
            dependent: exactly one instrument spec '<id>[|transform]', see tool description.
            regressors: comma-separated instrument specs, 1 to 10, see tool description for syntax.
            start: inclusive start date YYYY-MM-DD, empty for the earliest available date.
            end: inclusive end date YYYY-MM-DD, empty for the most recent available date.
            frequency: resample frequency, one of D, W, M, Q; defaults to D (native daily grid).
            robust_se: nonrobust, HC0, HC1, HC2, HC3, or HAC (Newey-West); default nonrobust.
            hac_lags: HAC lag count; 0 selects the automatic Newey-West rule; ignored otherwise.
            standardize: z-score dependent and regressors before fitting; default false.

        Returns:
            AnalysisResult JSON with payload.coefficients (per name: coef,
            std_err, t_stat, p_value, ci_low, ci_high), r_squared,
            adj_r_squared, f_stat, aic, bic, durbin_watson and
            residual_diagnostics — never the raw series or residuals.
        """
        if hac_lags < 0:
            raise ValueError("hac_lags must be zero (automatic) or positive")
        dataset, dep_label, reg_labels = self._load_regression(
            dependent, regressors, start=start, end=end, frequency=frequency
        )
        payload = _lazyregression().fit_ols(
            _as_lazystats_dataset(dataset),
            dep_label,
            reg_labels,
            cov=robust_se,
            hac_lags=hac_lags or None,
            standardize=standardize,
        )
        payload["data"] = _data_metadata(dataset)
        return _analysis_json(
            kind="report",
            produced_by="lazytools.statistical_analysis.regression_ols",
            instruments=dataset.instruments,
            payload=payload,
        )

    def _regression_ridge(
        self,
        dependent: str,
        regressors: str,
        start: str = "",
        end: str = "",
        frequency: str = "D",
        alpha: str = "",
        cv_folds: int = 5,
    ) -> str:
        """Ridge regression (scikit-learn, standardized regressors) between hub series.

        The dependent variable and every regressor are read exclusively from
        market-data-hub inside this tool — never pass raw series as an
        argument. Regressors are standardized before fitting and coefficients
        are back-transformed to original units; standardized_coefficients in
        the result are the ones actually shrunk by the penalty.

        Args:
            dependent: exactly one instrument spec '<id>[|transform]', see tool description.
            regressors: comma-separated instrument specs, 1 to 10, see tool description for syntax.
            start: inclusive start date YYYY-MM-DD, empty for the earliest available date.
            end: inclusive end date YYYY-MM-DD, empty for the most recent available date.
            frequency: resample frequency, one of D, W, M, Q; defaults to D (native daily grid).
            alpha: fixed penalty strength, or empty to select it by cross-validation (default).
            cv_folds: number of folds used only when alpha is empty; default 5.

        Returns:
            AnalysisResult JSON with payload.alpha, payload.alpha_selection
            (fixed or cv), payload.coefficients (original units),
            payload.standardized_coefficients and r_squared — never the raw
            series.
        """
        dataset, dep_label, reg_labels = self._load_regression(
            dependent, regressors, start=start, end=end, frequency=frequency
        )
        payload = _lazyregression().fit_ridge(
            _as_lazystats_dataset(dataset),
            dep_label,
            reg_labels,
            alpha=_parse_alpha(alpha),
            cv_folds=cv_folds,
        )
        payload["data"] = _data_metadata(dataset)
        return _analysis_json(
            kind="report",
            produced_by="lazytools.statistical_analysis.regression_ridge",
            instruments=dataset.instruments,
            payload=payload,
        )

    def _regression_lasso(
        self,
        dependent: str,
        regressors: str,
        start: str = "",
        end: str = "",
        frequency: str = "D",
        alpha: str = "",
        cv_folds: int = 5,
    ) -> str:
        """Lasso regression (scikit-learn, variable selection) between hub series.

        The dependent variable and every regressor are read exclusively from
        market-data-hub inside this tool — never pass raw series as an
        argument. Regressors are standardized before fitting; regressors
        the penalty shrinks exactly to zero are dropped from
        selected_regressors, which is how this tool answers "which of these
        actually matter".

        Args:
            dependent: exactly one instrument spec '<id>[|transform]', see tool description.
            regressors: comma-separated instrument specs, 1 to 10, see tool description for syntax.
            start: inclusive start date YYYY-MM-DD, empty for the earliest available date.
            end: inclusive end date YYYY-MM-DD, empty for the most recent available date.
            frequency: resample frequency, one of D, W, M, Q; defaults to D (native daily grid).
            alpha: fixed penalty strength, or empty to select it by cross-validation (default).
            cv_folds: number of folds used only when alpha is empty; default 5.

        Returns:
            AnalysisResult JSON with payload.alpha, payload.alpha_selection
            (fixed or cv), payload.coefficients, payload.n_nonzero,
            payload.selected_regressors and r_squared — never the raw series.
        """
        dataset, dep_label, reg_labels = self._load_regression(
            dependent, regressors, start=start, end=end, frequency=frequency
        )
        payload = _lazyregression().fit_lasso(
            _as_lazystats_dataset(dataset),
            dep_label,
            reg_labels,
            alpha=_parse_alpha(alpha),
            cv_folds=cv_folds,
        )
        payload["data"] = _data_metadata(dataset)
        return _analysis_json(
            kind="report",
            produced_by="lazytools.statistical_analysis.regression_lasso",
            instruments=dataset.instruments,
            payload=payload,
        )

    def _load_regression(
        self, dependent: str, regressors: str, *, start: str, end: str, frequency: str
    ) -> tuple[ReturnDataset, str, list[str]]:
        """One hub fetch for dependent + regressors; returns canonical labels.

        The dataset column labels are the backend's canonicalised instrument
        ids (bare symbols become ``ticker:<SYM>``), so the fit is called with
        the labels actually present in the panel, in request order:
        dependent first, then each regressor.
        """
        dependent = dependent.strip()
        if not dependent or "," in dependent:
            raise ValueError("dependent must be exactly one instrument spec")
        requested = [item.strip() for item in regressors.split(",") if item.strip()]
        if not requested:
            raise ValueError("regressors must contain at least one instrument spec")
        if len(requested) > _MAX_REGRESSORS:
            raise ValueError(f"at most {_MAX_REGRESSORS} regressors are supported")
        labels = [_spec_label(item) for item in [dependent, *requested]]
        if len(set(labels)) != len(labels):
            raise ValueError("dependent and regressors must be unique")
        dataset = self._load(
            ",".join([dependent, *requested]), start=start, end=end, frequency=frequency
        )
        return dataset, labels[0], labels[1:]

    def _load(self, instruments: str, *, start: str, end: str, frequency: str) -> ReturnDataset:
        if frequency not in _PERIODS_PER_YEAR:
            raise ValueError("frequency must be one of D, W, M, Q")
        backend = self._resolve()
        loader = getattr(backend, "load_series", None)
        if loader is None:
            # legacy backend (pre-transformation-layer): plain return specs only
            if any("|" in item for item in instruments.split(",")):
                raise ValueError(
                    "this data backend does not support '|<transform>' specs; "
                    "pass plain instrument ids"
                )
            return backend.load_returns(instruments, start=start, end=end, frequency=frequency)
        return loader(instruments, start=start, end=end, frequency=frequency)


def _data_metadata(dataset: ReturnDataset) -> dict[str, Any]:
    """Return bounded provenance/coverage metadata, never raw observations.

    This is deliberately an allow-list rather than copying backend metadata:
    the complete return matrix must remain inside the tool process and never be
    added to the LLM tool result accidentally by a future backend change.
    """
    allowed = (
        "source",
        "domain",
        "field",
        "transform",
        "series",
        "frequency",
        "return_kind",
        "n_rows",
        "n_cols",
        "columns",
        "missing",
        "missing_pct",
        "date_start",
        "date_end",
        "used_returns_view",
        "requested_start",
        "requested_end",
        "instruments",
    )
    return {key: dataset.metadata[key] for key in allowed if key in dataset.metadata}


def _spec_label(spec: str) -> str:
    """Canonical column label of a '<id>[|<transform>]' spec.

    Mirrors the backend's bare-symbol canonicalisation (``SPY`` →
    ``ticker:SPY``) so regression labels always match the panel columns.
    """
    identifier = spec.partition("|")[0].strip()
    return identifier if ":" in identifier else f"ticker:{identifier}"


def _parse_alpha(alpha: str) -> float | None:
    """LLM-facing alpha: empty string selects cross-validation."""
    text = alpha.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError("alpha must be a number or empty for cross-validation") from exc


def _drop_meaningless_annualization(payload: dict[str, Any], dataset: ReturnDataset) -> None:
    """Null the sqrt-of-time annualization for non-return series.

    The core statistic annualizes every column; scaling the standard
    deviation of a level/diff series by sqrt(periods_per_year) is not a
    volatility, so it is dropped here at the bridge. Datasets without
    per-series transform metadata (legacy backends) are all returns.
    """
    series_info = dataset.metadata.get("series")
    if not isinstance(series_info, dict):
        return
    for instrument, stats in payload.get("volatility", {}).items():
        transform = series_info.get(instrument, {}).get("transform")
        if transform not in _RETURN_TRANSFORMS:
            stats["annualized_volatility"] = None
            stats["mean_value"] = stats.pop("mean_log_return", None)
            stats["period_std"] = stats.pop("period_volatility", None)


def _round(value: float) -> float:
    return round(float(value), 10)


def _analysis_json(
    *,
    kind: str,
    produced_by: str,
    instruments: list[str],
    payload: dict[str, Any],
) -> str:
    """Serialise through lazydatacore when the hub is installed.

    The small plain-JSON fallback keeps custom fake backends testable without
    installing the private data hub. The default backend cannot run without it.
    """
    try:
        from market_data_hub.lazydatacore import (
            AnalysisResult,
            InstrumentId,
            Provenance,
            ResultKind,
            SourceRef,
            now_utc,
        )
    except ImportError:
        return json.dumps(
            {
                "kind": kind,
                "produced_by": produced_by,
                "instruments": instruments,
                "payload": payload,
                "provenance": {"source": {"source": "market-data-hub", "content_is_untrusted": False}},
            },
            ensure_ascii=False,
        )

    result = AnalysisResult(
        kind=ResultKind(kind),
        produced_by=produced_by,
        instruments=[InstrumentId.parse(item) for item in instruments],
        payload=payload,
        provenance=Provenance(
            source=SourceRef(source="market-data-hub", content_is_untrusted=False),
            as_of=now_utc(),
            tool_version=_TOOL_VERSION,
        ),
    )
    return result.model_dump_json()
