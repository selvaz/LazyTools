"""Read Yahoo Finance statements in memory and compare them with filing data.

The connector has no Store, no agent tools, and no DataHub integration.  It is
an explicit, caller-owned comparison source: the filing-derived
``NormalisedBase`` remains authoritative, while a vendor disagreement is a
review signal rather than an automatic replacement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isclose
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Literal

from lazytools.financials.normalised import NormalisedBase

Frequency = Literal["annual", "quarterly"]
ComparisonStatus = Literal["match", "mismatch", "missing_primary", "missing_vendor"]


class YahooFundamentalsError(RuntimeError):
    """Yahoo Finance returned no usable fundamentals or changed its shape."""


@dataclass(frozen=True)
class StatementSnapshot:
    """One Yahoo statement for one reporting date, held only in memory."""

    statement: Literal["income", "balance", "cash_flow"]
    period_end: date
    values: dict[str, float]


@dataclass(frozen=True)
class YahooFundamentals:
    """All readable statements for one ticker and one requested frequency."""

    ticker: str
    frequency: Frequency
    statements: tuple[StatementSnapshot, ...]

    def statement(self, kind: Literal["income", "balance", "cash_flow"], period_end: date) -> StatementSnapshot | None:
        return next((item for item in self.statements if item.statement == kind and item.period_end == period_end), None)


@dataclass(frozen=True)
class VendorComparison:
    """A comparison finding; it never selects a vendor value as the truth."""

    metric: str
    status: ComparisonStatus
    filing_value: float | None
    vendor_value: float | None
    detail: str


# The mapping is intentionally short and auditable.  A new vendor label is not
# silently treated as a known accounting metric; add it here with a regression
# test after inspecting an actual statement.
_VENDOR_LINES: dict[str, tuple[str, tuple[str, ...]]] = {
    "revenue": ("income", ("Total Revenue",)),
    "operating_income": ("income", ("Operating Income", "Operating Income Loss")),
    "cfo": ("cash_flow", ("Operating Cash Flow", "Total Cash From Operating Activities")),
    "cash_and_equivalents": ("balance", (
        "Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents",
    )),
    "short_term_borrowings": ("balance", ("Current Debt",)),
    "long_term_debt_noncurrent": ("balance", ("Long Term Debt",)),
}


class YahooFundamentalsClient:
    """Small synchronous wrapper around :mod:`yfinance` statement accessors.

    ``ticker_factory`` is injectable so tests never contact Yahoo.  The default
    import is lazy, making the ``yahoo`` extra optional for users who do not use
    this connector.
    """

    def __init__(
        self,
        ticker_factory: Callable[[str], Any] | None = None,
        *,
        cache_dir: str | Path | None = None,
    ) -> None:
        self._ticker_factory = ticker_factory
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None

    def fetch(self, ticker: str, *, frequency: Frequency = "annual") -> YahooFundamentals:
        if frequency not in {"annual", "quarterly"}:
            raise ValueError("frequency must be 'annual' or 'quarterly'")
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker must not be empty")
        instrument = self._instrument(symbol)
        attributes = (
            ("income", "income_stmt" if frequency == "annual" else "quarterly_income_stmt"),
            ("balance", "balance_sheet" if frequency == "annual" else "quarterly_balance_sheet"),
            ("cash_flow", "cash_flow" if frequency == "annual" else "quarterly_cash_flow"),
        )
        snapshots: list[StatementSnapshot] = []
        for kind, attribute in attributes:
            snapshots.extend(_statement_snapshots(kind, getattr(instrument, attribute, None)))
        if not snapshots:
            raise YahooFundamentalsError(f"Yahoo returned no {frequency} statements for {symbol}")
        return YahooFundamentals(symbol, frequency, tuple(snapshots))

    def _instrument(self, ticker: str) -> Any:
        if self._ticker_factory is not None:
            return self._ticker_factory(ticker)
        try:
            import yfinance
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise YahooFundamentalsError(
                "yfinance is required; install lazytoolkit[yahoo]"
            ) from exc
        if self._cache_dir is not None:
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                # yfinance otherwise writes its cookie cache under the user's
                # profile.  This directory contains session metadata only; the
                # connector itself retains no statement data after ``fetch``.
                yfinance.set_tz_cache_location(str(self._cache_dir))
            except OSError as exc:
                raise YahooFundamentalsError(
                    f"cannot create the requested yfinance cache directory: {self._cache_dir}"
                ) from exc
        return yfinance.Ticker(ticker)


def compare_with_base(
    base: NormalisedBase,
    vendor: YahooFundamentals,
    *,
    relative_tolerance: float = 0.02,
) -> tuple[VendorComparison, ...]:
    """Compare the overlapping annual or quarterly vendor values with a base.

    A vendor snapshot whose period does not equal the filing period is excluded:
    a trailing-twelve-month value is not a check on an annual filing.  Values
    never flow back into ``base`` and are never persisted.
    """
    if relative_tolerance < 0:
        raise ValueError("relative_tolerance must be non-negative")
    findings: list[VendorComparison] = []
    for metric, (statement, labels) in _VENDOR_LINES.items():
        primary = base.get(metric)
        snapshot = vendor.statement(statement, base.period_end)
        vendor_value = _first_value(snapshot, labels) if snapshot else None
        if primary is None or not primary.usable:
            findings.append(VendorComparison(
                metric, "missing_primary", None, vendor_value,
                "the filing-derived base has no usable value",
            ))
        elif vendor_value is None:
            findings.append(VendorComparison(
                metric, "missing_vendor", primary.value, None,
                "Yahoo did not return the mapped line for the same period",
            ))
        else:
            findings.append(VendorComparison(
                metric,
                "match" if isclose(primary.value or 0.0, vendor_value, rel_tol=relative_tolerance) else "mismatch",
                primary.value, vendor_value,
                f"Yahoo {statement} statement for {base.period_end.isoformat()}",
            ))
    return tuple(findings)


def _statement_snapshots(kind: str, frame: Any) -> list[StatementSnapshot]:
    """Convert a pandas-like yfinance frame without importing pandas directly."""
    if frame is None or getattr(frame, "empty", True):
        return []
    snapshots: list[StatementSnapshot] = []
    for column in frame.columns:
        period_end = _as_date(column)
        if period_end is None:
            continue
        values: dict[str, float] = {}
        for label, raw in frame[column].items():
            # pandas commonly hands back NumPy scalar types, which are real
            # numbers but are not consistently instances of Python ``float``.
            if isinstance(raw, Real) and not isinstance(raw, bool):
                values[str(label)] = float(raw)
        if values:
            snapshots.append(StatementSnapshot(kind, period_end, values))
    return snapshots


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _first_value(snapshot: StatementSnapshot | None, labels: tuple[str, ...]) -> float | None:
    if snapshot is None:
        return None
    for label in labels:
        if label in snapshot.values:
            return snapshot.values[label]
    return None


__all__ = [
    "StatementSnapshot",
    "VendorComparison",
    "YahooFundamentals",
    "YahooFundamentalsClient",
    "YahooFundamentalsError",
    "compare_with_base",
]
