"""Read-only tools over market-data-hub's CFTC positioning data."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _records(frame: pd.DataFrame) -> list[dict]:
    """A hub reader's DataFrame as plain, JSON-safe dicts.

    ``DataFrame.to_dict(orient="records")`` alone is not enough: the hub's
    readers return a real ``report_date`` column and ``NaN`` for
    legitimately-absent position figures (a COT report does not populate
    every trader category for every contract) -- neither is
    JSON-serializable as-is. Dates become ISO ``YYYY-MM-DD`` strings; every
    other null becomes ``None``. Same fix as the sibling
    ``treasury_fiscal.py`` connector's ``_records`` helper.
    """
    frame = frame.copy()
    for column in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            frame[column] = frame[column].dt.strftime("%Y-%m-%d")
    frame = frame.astype(object).where(pd.notnull(frame), None)
    return frame.to_dict(orient="records")


class CFTCPositioningTools:
    """A LazyBridge ``ToolProvider`` over the hub's CFTC COT readers.

    **Why these methods restate their arguments instead of wrapping the hub's
    functions directly.** The hub readers accept a database path that the
    provider must bind because a model cannot supply or manage the underlying
    database connection. Their Python-facing optional parameters also use
    ``None``, which does not translate reliably into an LLM tool schema. This
    thin layer therefore uses required date strings and ``""`` for an absent
    contract filter, translates the latter to ``None``, and converts each
    returned DataFrame into JSON-shaped records.

    It adds no write surface: market-data-hub's ingestion job owns the data,
    and these tools only read it.
    """

    _is_lazy_tool_provider = True

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    def cftc_positioning_financial(
        self,
        start: str,
        end: str,
        contract_market_name: str = "",
    ) -> dict:
        """Financial futures ONLY (rates, FX, equity indices, credit), with dealer/asset-manager/leveraged-money breakdowns; for energy, metals, or agricultural commodities use ``cftc_positioning_commodities`` instead, which has a coarser commercial/non-commercial split and no leveraged-money category.

        Args:
            start: Inclusive first report date in YYYY-MM-DD format; this required value cannot be empty.
            end: Inclusive last report date in YYYY-MM-DD format; this required value cannot be empty.
            contract_market_name: Exact contract market name to filter on, or an empty string for all financial contracts.
        """
        if not start or not start.strip():
            raise ValueError("start is required")
        if not end or not end.strip():
            raise ValueError("end is required")

        from market_data_hub.reader import read_cftc_tff

        contract_filter = contract_market_name or None
        rows = read_cftc_tff(
            start=start,
            end=end,
            contract_market_name=contract_filter,
            db_path=self._db_path,
        )
        rows = _records(rows)
        return {
            "start": start,
            "end": end,
            "contract_market_name": contract_filter,
            "returned": len(rows),
            "rows": rows,
        }

    def cftc_positioning_commodities(
        self,
        start: str,
        end: str,
        contract_market_name: str = "",
    ) -> dict:
        """Commodity futures ONLY (including energy, metals, and agriculture), with a coarser commercial/non-commercial split and no dealer/asset-manager/leveraged-money breakdown; use ``cftc_positioning_financial`` for financial futures and those detailed trader categories.

        Args:
            start: Inclusive first report date in YYYY-MM-DD format; this required value cannot be empty.
            end: Inclusive last report date in YYYY-MM-DD format; this required value cannot be empty.
            contract_market_name: Exact contract market name to filter on, or an empty string for all commodity contracts.
        """
        if not start or not start.strip():
            raise ValueError("start is required")
        if not end or not end.strip():
            raise ValueError("end is required")

        from market_data_hub.reader import read_cftc_legacy

        contract_filter = contract_market_name or None
        rows = read_cftc_legacy(
            start=start,
            end=end,
            contract_market_name=contract_filter,
            db_path=self._db_path,
        )
        rows = _records(rows)
        return {
            "start": start,
            "end": end,
            "contract_market_name": contract_filter,
            "returned": len(rows),
            "rows": rows,
        }

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(
                self.cftc_positioning_financial,
                name="cftc_positioning_financial",
            ),
            Tool.wrap(
                self.cftc_positioning_commodities,
                name="cftc_positioning_commodities",
            ),
        ]
