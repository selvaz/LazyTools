"""Read-only return-data seam backed exclusively by market-data-hub.

The statistical tools deliberately do not accept price vectors from an LLM.
Their observations are always read from the shared market-data-hub warehouse,
which keeps the calculation reproducible and avoids a second data source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ReturnDataset:
    """Normalised log-return observations produced by the data hub.

    ``rows`` contains ``{"date": "YYYY-MM-DD", "<instrument>": float | None}``
    mappings. Instruments are canonical lazydatacore IDs, not warehouse keys.
    """

    instruments: list[str]
    rows: list[dict[str, Any]]
    metadata: dict[str, Any]


class StatisticalDataBackend(Protocol):
    """Minimal read-only interface required by :class:`StatisticalAnalysisTools`."""

    def load_returns(
        self,
        instruments: str,
        *,
        start: str = "",
        end: str = "",
        frequency: str = "D",
    ) -> ReturnDataset: ...

    def load_series(
        self,
        specs: str,
        *,
        start: str = "",
        end: str = "",
        frequency: str = "D",
    ) -> ReturnDataset: ...


class MarketDataHubStatisticsBackend:
    """Load complete return histories from market-data-hub.

    The market-data-hub dependency is imported only on first use. The backend
    calls ``extract.extract_returns`` directly rather than its agent JSON tool:
    the latter intentionally truncates long responses for model context, which
    would make volatility, correlation and z-scores mathematically incorrect.
    """

    def load_returns(
        self,
        instruments: str,
        *,
        start: str = "",
        end: str = "",
        frequency: str = "D",
    ) -> ReturnDataset:
        try:
            from market_data_hub import extract
            from market_data_hub.lazydatacore import Domain, InstrumentId
        except ImportError as exc:  # pragma: no cover - depends on private package
            raise ImportError(
                "StatisticalAnalysisTools requires market-data-hub (the shared data hub): "
                "pip install 'market-data-hub @ git+https://github.com/selvaz/market-data-hub.git'"
            ) from exc

        requested = [item.strip() for item in instruments.split(",") if item.strip()]
        if not requested:
            raise ValueError("instruments must contain at least one market-data-hub ticker")

        parsed = [InstrumentId.parse(item if ":" in item else f"ticker:{item}") for item in requested]
        unsupported = [str(item) for item in parsed if item.domain is not Domain.TICKER]
        if unsupported:
            raise ValueError(
                "return statistics currently support market-data-hub price instruments only "
                f"(ticker:<symbol>); unsupported: {', '.join(unsupported)}"
            )
        if len({str(item) for item in parsed}) != len(parsed):
            raise ValueError("instruments must be unique after lazydatacore canonicalisation")

        if frequency not in {"D", "W", "M", "Q"}:
            raise ValueError("frequency must be one of D, W, M, Q")

        symbols = [item.key for item in parsed]
        frame, metadata = extract.extract_returns(
            symbols,
            start=start or None,
            end=end or None,
            frequency=frequency,
        )
        labels = [str(item) for item in parsed]
        symbol_to_label = dict(zip(symbols, labels, strict=True))

        rows: list[dict[str, Any]] = []
        for timestamp, values in frame.iterrows():
            row: dict[str, Any] = {"date": timestamp.date().isoformat()}
            for symbol, label in symbol_to_label.items():
                value = values.get(symbol)
                row[label] = None if value is None or not _is_finite(value) else float(value)
            rows.append(row)

        return ReturnDataset(
            instruments=labels,
            rows=rows,
            metadata={
                **metadata,
                "instruments": labels,
                "requested_start": start or None,
                "requested_end": end or None,
                "frequency": frequency,
                "return_kind": "log",
                "source": "market-data-hub",
            },
        )


    # ------------------------------------------------------------------
    # Generic transformation layer: any hub series, per-instrument transform
    # ------------------------------------------------------------------

    def load_series(
        self,
        specs: str,
        *,
        start: str = "",
        end: str = "",
        frequency: str = "D",
    ) -> ReturnDataset:
        """Load a mixed-domain panel with per-instrument transforms.

        ``specs`` is a comma-separated list of ``<instrument>[|<transform>]``
        items, e.g. ``"ticker:SPY, ticker:AAPL|level, macro:FEDFUNDS|diff,
        factor:FF5_daily/Mkt-RF"``. Transforms are the hub's own
        (``level|log_return|pct_change|diff``); when omitted, the default is
        the return-flavoured one per domain: ``log_return`` for tickers,
        ``level`` for factors (already stored as decimal returns) and
        ``diff`` for macro series (log/pct are undefined on series that cross
        zero). Columns are outer-joined on dates — pairwise statistics and
        regression alignment happen downstream on the shared panel.
        """
        try:
            import pandas as pd
            from market_data_hub import extract, reader
            from market_data_hub.lazydatacore import Domain, InstrumentId
        except ImportError as exc:  # pragma: no cover - depends on private package
            raise ImportError(
                "StatisticalAnalysisTools requires market-data-hub (the shared data hub): "
                "pip install 'market-data-hub @ git+https://github.com/selvaz/market-data-hub.git'"
            ) from exc

        if frequency not in _FREQUENCIES:
            raise ValueError("frequency must be one of D, W, M, Q")

        parsed = _parse_specs(specs, Domain, InstrumentId)
        window = {"start": start or None, "end": end or None}

        columns: dict[str, Any] = {}
        # tickers on the default return path share one extract_returns call
        # (the hub's stored-returns fast path); everything else goes through
        # extract_series / read_factors grouped by (domain, transform).
        return_tickers = [
            item for item in parsed if item.domain is Domain.TICKER and item.transform == "log_return"
        ]
        if return_tickers:
            frame, _ = extract.extract_returns(
                [item.instrument.key for item in return_tickers],
                frequency=frequency,
                **window,
            )
            for item in return_tickers:
                columns[item.label] = frame.get(item.instrument.key)

        level_tickers = [
            item for item in parsed if item.domain is Domain.TICKER and item.transform != "log_return"
        ]
        for transform in sorted({item.transform for item in level_tickers}):
            group = [item for item in level_tickers if item.transform == transform]
            frame, _ = extract.extract_series(
                [item.instrument.key for item in group],
                domain="prices",
                transform=transform,
                frequency=frequency,
                **window,
            )
            for item in group:
                columns[item.label] = frame.get(item.instrument.key)

        macro = [item for item in parsed if item.domain is Domain.MACRO]
        for transform in sorted({item.transform for item in macro}):
            group = [item for item in macro if item.transform == transform]
            frame, _ = extract.extract_series(
                [item.instrument.key for item in group],
                domain="macro",
                transform=transform,
                frequency=frequency,
                **window,
            )
            for item in group:
                columns[item.label] = frame.get(item.instrument.key)

        factors = [item for item in parsed if item.domain is Domain.FACTOR]
        factor_sets = sorted({item.instrument.key.split("/", 1)[0] for item in factors})
        for factor_set in factor_sets:
            group = [item for item in factors if item.instrument.key.startswith(f"{factor_set}/")]
            names = [item.instrument.key.split("/", 1)[1] for item in group]
            frame = reader.read_factors(
                factors=names, factor_set=factor_set, wide=True, **window
            )
            frame = _compound_factor_returns(frame, frequency, pd)
            for item, name in zip(group, names, strict=True):
                columns[item.label] = frame.get(name) if not frame.empty else None

        labels = [item.label for item in parsed]
        panel = pd.DataFrame(
            {label: series for label, series in columns.items() if series is not None}
        ).sort_index()
        panel = panel.dropna(how="all")

        rows: list[dict[str, Any]] = []
        for timestamp, values in panel.iterrows():
            row: dict[str, Any] = {"date": timestamp.date().isoformat()}
            for label in labels:
                value = values.get(label)
                row[label] = float(value) if value is not None and _is_finite(value) else None
            rows.append(row)

        series_info = {
            item.label: {"domain": item.domain.value, "transform": item.transform}
            for item in parsed
        }
        return ReturnDataset(
            instruments=labels,
            rows=rows,
            metadata={
                "source": "market-data-hub",
                "instruments": labels,
                "series": series_info,
                "requested_start": start or None,
                "requested_end": end or None,
                "frequency": frequency,
                "n_rows": len(rows),
                "n_cols": len(labels),
                "columns": labels,
                "date_start": rows[0]["date"] if rows else None,
                "date_end": rows[-1]["date"] if rows else None,
            },
        )


_TRANSFORMS = {"level", "log_return", "pct_change", "diff"}
_FREQUENCIES = {"D", "W", "M", "Q"}
# Resampling rules for compounding factor returns — same targets as the hub's
# extract layer (W-FRI/month-end/quarter-end).
_FACTOR_FREQ_RULE = {"W": "W-FRI", "M": "ME", "Q": "QE"}


@dataclass(frozen=True)
class _SeriesSpec:
    instrument: Any  # InstrumentId
    label: str
    domain: Any  # Domain
    transform: str


def _parse_specs(specs: str, domain_enum: Any, instrument_id: Any) -> list[_SeriesSpec]:
    requested = [item.strip() for item in specs.split(",") if item.strip()]
    if not requested:
        raise ValueError("specs must contain at least one market-data-hub instrument")

    defaults = {
        domain_enum.TICKER: "log_return",
        domain_enum.FACTOR: "level",
        domain_enum.MACRO: "diff",
    }
    parsed: list[_SeriesSpec] = []
    for item in requested:
        identifier, _, transform = (part.strip() for part in item.partition("|"))
        instrument = instrument_id.parse(
            identifier if ":" in identifier else f"ticker:{identifier}"
        )
        if instrument.domain not in defaults:
            raise ValueError(
                "series statistics support ticker:, factor: and macro: instruments only; "
                f"unsupported: {instrument}"
            )
        if transform and transform not in _TRANSFORMS:
            raise ValueError(
                f"unknown transform {transform!r} for {instrument}; "
                f"allowed: {', '.join(sorted(_TRANSFORMS))}"
            )
        if instrument.domain is domain_enum.FACTOR:
            if "/" not in instrument.key:
                raise ValueError(
                    f"factor instruments need a 'factor_set/factor' key, got {instrument}"
                )
            if transform and transform != "level":
                raise ValueError(
                    f"factor series are already returns; only 'level' is valid, got {transform!r}"
                )
        parsed.append(
            _SeriesSpec(
                instrument=instrument,
                label=str(instrument),
                domain=instrument.domain,
                transform=transform or defaults[instrument.domain],
            )
        )

    labels = [item.label for item in parsed]
    if len(set(labels)) != len(labels):
        raise ValueError("instruments must be unique after lazydatacore canonicalisation")
    return parsed


def _compound_factor_returns(frame: Any, frequency: str, pd: Any) -> Any:
    """Resample stored decimal factor returns by compounding.

    The hub's extract layer resamples LEVELS with ``last()`` and transforms
    afterwards, which is wrong for series that are already returns — a weekly
    factor return is the compound of its days, not Friday's print. Empty
    buckets stay NaN (``min_count=1``) instead of becoming fake zeros.
    """
    if frame.empty or frequency == "D":
        return frame
    import numpy as np

    compounded = np.expm1(
        np.log1p(frame).resample(_FACTOR_FREQ_RULE[frequency]).sum(min_count=1)
    )
    return compounded


def _is_finite(value: Any) -> bool:
    """Accept Python/numpy real values while turning pandas NaN into ``None``."""
    import math

    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
