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


def _is_finite(value: Any) -> bool:
    """Accept Python/numpy real values while turning pandas NaN into ``None``."""
    import math

    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
