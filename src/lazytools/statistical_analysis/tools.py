"""Volatility, correlation and return-outlier tools for LazyBridge agents."""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable
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
                    "Calculate per-instrument standard deviation of log returns and annualised "
                    "volatility from market-data-hub only. Args: instruments (comma-separated "
                    "canonical IDs, e.g. 'ticker:SPY,ticker:TLT'), start/end (YYYY-MM-DD, "
                    "optional), frequency (D|W|M|Q, default D). Returns an AnalysisResult JSON."
                ),
            ),
            Tool.wrap(
                self._return_correlation,
                name="statistical_return_correlation",
                description=(
                    "Calculate pairwise Pearson correlations between log returns read only from "
                    "market-data-hub. Args: instruments, start/end (YYYY-MM-DD, optional), "
                    "frequency (D|W|M|Q, default D), min_periods (default 2). Returns an "
                    "AnalysisResult JSON with correlation and pairwise-observation matrices."
                ),
            ),
            Tool.wrap(
                self._return_outliers,
                name="statistical_return_outliers",
                description=(
                    "Find outlier observations in log returns read only from market-data-hub. "
                    "For each instrument, z-score = (return - selected-period mean) / selected-"
                    "period sample standard deviation; flags abs(z-score) >= threshold. Args: "
                    "instruments, start/end (YYYY-MM-DD, optional), frequency (D|W|M|Q, default "
                    "D), threshold (absolute z-score, default 2), max_results (default 100, "
                    "hard cap 250). "
                    "Returns an AnalysisResult JSON including dates, returns and z-scores."
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
        dataset = self._load(instruments, start=start, end=end, frequency=frequency)
        period_factor = _periods_per_year(frequency)
        volatility: dict[str, dict[str, float | int | None]] = {}
        for instrument, values in _series_values(dataset).items():
            n = len(values)
            if n < 2:
                volatility[instrument] = {
                    "observations": n,
                    "mean_log_return": None,
                    "period_volatility": None,
                    "annualized_volatility": None,
                }
                continue
            sigma = statistics.stdev(values)
            volatility[instrument] = {
                "observations": n,
                "mean_log_return": _round(statistics.fmean(values)),
                "period_volatility": _round(sigma),
                "annualized_volatility": _round(sigma * math.sqrt(period_factor)),
            }
        return _analysis_json(
            kind="report",
            produced_by="lazytools.statistical_analysis.return_volatility",
            instruments=dataset.instruments,
            payload={
                "metric": "sample standard deviation of log returns",
                "frequency": frequency,
                "periods_per_year": period_factor,
                "volatility": volatility,
                "data": _data_metadata(dataset),
            },
        )

    def _return_correlation(
        self,
        instruments: str,
        start: str = "",
        end: str = "",
        frequency: str = "D",
        min_periods: int = 2,
    ) -> str:
        if min_periods < 2:
            raise ValueError("min_periods must be at least 2")
        dataset = self._load(instruments, start=start, end=end, frequency=frequency)
        observations = _series_observations(dataset)
        correlation: dict[str, dict[str, float | None]] = {}
        pair_counts: dict[str, dict[str, int]] = {}
        for left in dataset.instruments:
            correlation[left] = {}
            pair_counts[left] = {}
            for right in dataset.instruments:
                paired = [
                    (values[left], values[right])
                    for _, values in observations
                    if left in values and right in values
                ]
                pair_counts[left][right] = len(paired)
                correlation[left][right] = _pearson(paired) if len(paired) >= min_periods else None
        return _analysis_json(
            kind="report",
            produced_by="lazytools.statistical_analysis.return_correlation",
            instruments=dataset.instruments,
            payload={
                "metric": "Pearson correlation of log returns",
                "frequency": frequency,
                "min_periods": min_periods,
                "correlation": correlation,
                "pairwise_observations": pair_counts,
                "data": _data_metadata(dataset),
            },
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
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError("threshold must be a finite value greater than zero")
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        dataset = self._load(instruments, start=start, end=end, frequency=frequency)
        series = _series_values(dataset)
        z_scores: dict[str, tuple[float, float] | None] = {}
        for instrument, values in series.items():
            if len(values) < 2:
                z_scores[instrument] = None
                continue
            sigma = statistics.stdev(values)
            z_scores[instrument] = None if sigma == 0 else (statistics.fmean(values), sigma)

        outliers: list[dict[str, Any]] = []
        for date, observation_values in _series_observations(dataset):
            for instrument in dataset.instruments:
                value = observation_values.get(instrument)
                params = z_scores[instrument]
                if value is None or params is None:
                    continue
                mean, sigma = params
                z_score = (value - mean) / sigma
                if abs(z_score) >= threshold:
                    outliers.append(
                        {
                            "date": date,
                            "instrument": instrument,
                            "log_return": _round(value),
                            "z_score": _round(z_score),
                            "direction": "positive" if z_score > 0 else "negative",
                        }
                    )

        outliers.sort(key=lambda item: (-abs(float(item["z_score"])), item["date"], item["instrument"]))
        total_outliers = len(outliers)
        returned = outliers[: min(max_results, _MAX_OUTLIER_RESULTS)]
        return _analysis_json(
            kind="signal",
            produced_by="lazytools.statistical_analysis.return_outliers",
            instruments=dataset.instruments,
            payload={
                "metric": "period z-score of log returns",
                "frequency": frequency,
                "threshold": threshold,
                "comparison": "abs(z_score) >= threshold",
                "total_outliers": total_outliers,
                "returned_outliers": len(returned),
                "truncated": total_outliers > len(returned),
                "outliers": returned,
                "data": _data_metadata(dataset),
            },
        )

    def _load(self, instruments: str, *, start: str, end: str, frequency: str) -> ReturnDataset:
        if frequency not in _PERIODS_PER_YEAR:
            raise ValueError("frequency must be one of D, W, M, Q")
        return self._resolve().load_returns(instruments, start=start, end=end, frequency=frequency)


def _series_observations(dataset: ReturnDataset) -> list[tuple[str, dict[str, float]]]:
    """Validate backend rows and discard missing values, preserving date ordering."""
    observations: list[tuple[str, dict[str, float]]] = []
    for row in dataset.rows:
        date = row.get("date")
        if not isinstance(date, str) or not date:
            raise ValueError("market-data-hub return data must contain a non-empty date")
        values: dict[str, float] = {}
        for instrument in dataset.instruments:
            value = row.get(instrument)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"non-numeric return for {instrument!r} at {date}") from exc
            if math.isfinite(number):
                values[instrument] = number
        observations.append((date, values))
    return observations


def _series_values(dataset: ReturnDataset) -> dict[str, list[float]]:
    values: dict[str, list[float]] = {instrument: [] for instrument in dataset.instruments}
    for _, row_values in _series_observations(dataset):
        for instrument, value in row_values.items():
            values[instrument].append(value)
    return values


def _pearson(pairs: Iterable[tuple[float, float]]) -> float | None:
    values = list(pairs)
    if len(values) < 2:
        return None
    left, right = zip(*values, strict=True)
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in values)
    left_scale = math.sqrt(sum((x - left_mean) ** 2 for x in left))
    right_scale = math.sqrt(sum((y - right_mean) ** 2 for y in right))
    if left_scale == 0 or right_scale == 0:
        return None
    return _round(numerator / (left_scale * right_scale))


def _periods_per_year(frequency: str) -> int:
    try:
        return _PERIODS_PER_YEAR[frequency]
    except KeyError as exc:
        raise ValueError("frequency must be one of D, W, M, Q") from exc


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
