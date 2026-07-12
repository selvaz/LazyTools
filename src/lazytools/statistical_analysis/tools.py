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
        core, _ = _lazystats()
        dataset = self._load(instruments, start=start, end=end, frequency=frequency)
        payload = core.return_volatility(_as_lazystats_dataset(dataset), frequency=frequency)
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

    def _load(self, instruments: str, *, start: str, end: str, frequency: str) -> ReturnDataset:
        if frequency not in _PERIODS_PER_YEAR:
            raise ValueError("frequency must be one of D, W, M, Q")
        return self._resolve().load_returns(instruments, start=start, end=end, frequency=frequency)


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
