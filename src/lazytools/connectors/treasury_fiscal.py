"""Read-only tools over market-data-hub's Treasury Fiscal Data readers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd


def _records(frame: pd.DataFrame) -> list[dict]:
    """A hub reader's DataFrame as plain, JSON-safe dicts.

    ``DataFrame.to_dict(orient="records")`` alone is not enough: the hub's
    readers return real ``date``/``Timestamp`` columns and ``NaN``/``NaT``
    for legitimately-absent values (common on ``treasury_auctions``, where
    many numeric/date columns only apply to some ``security_type``s) --
    neither is JSON-serializable as-is. Dates become ISO ``YYYY-MM-DD``
    strings; every other null becomes ``None``.

    ``pandas`` is imported here, not at module level: this module must stay
    importable (e.g. by the MCP surface-contract test) even where
    market-data-hub -- and therefore pandas -- isn't installed, matching
    the local ``from market_data_hub.reader import ...`` imports below.
    """
    import pandas as pd

    frame = frame.copy()
    for column in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            frame[column] = frame[column].dt.strftime("%Y-%m-%d")
    frame = frame.astype(object).where(pd.notnull(frame), None)
    return frame.to_dict(orient="records")


class TreasuryFiscalTools:
    """A LazyBridge ``ToolProvider`` over the hub's Treasury Fiscal Data.

    **Why these methods restate their arguments instead of wrapping the hub's
    functions directly.** The hub owns downloading, ingestion, storage, and
    its Python-facing reader API. This class only translates that API into a
    shape an LLM tool can call.

    A model cannot supply an open database connection, and Python types such
    as ``Optional[str]`` do not express absence reliably in a JSON tool schema.
    The methods therefore bind the configured database path, accept strings,
    and translate ``""`` to no filter before forwarding to exactly one hub
    reader. If the hub grows an LLM-shaped bound interface, these methods can
    collapse into direct wraps and this note should go with them.

    Read-only throughout: the hub's ingestion job writes this data, never an
    agent using these tools.
    """

    _is_lazy_tool_provider = True

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    def treasury_cash_balance(
        self,
        start: str,
        end: str,
        account_type: str = "",
    ) -> dict:
        """Read Treasury cash balances in a date range; ``account_type`` groups several different figures under each ``record_date``, so opening balance, deposits, and withdrawals are separate rows rather than columns, and a caller seeking the TGA balance on a day must filter to the right account label instead of filtering only by date.

        Args:
            start: Required inclusive start date in YYYY-MM-DD format.
            end: Required inclusive end date in YYYY-MM-DD format.
            account_type: Exact account-type label to match, such as 'Treasury General Account (TGA) Opening Balance'; pass an empty string for all account types.
        """
        if not start or not start.strip():
            raise ValueError("start is required")
        if not end or not end.strip():
            raise ValueError("end is required")

        from market_data_hub.reader import read_treasury_cash_balance

        account_filter = account_type or None
        frame = read_treasury_cash_balance(
            start=start,
            end=end,
            account_type=account_filter,
            db_path=self._db_path,
        )
        rows = _records(frame)
        return {
            "start": start,
            "end": end,
            "account_type": account_filter,
            "returned": len(rows),
            "rows": rows,
        }

    def treasury_debt(self, start: str, end: str) -> dict:
        """Read daily Treasury debt totals in a required inclusive date range, including debt held by the public, intragovernmental holdings, and total public debt outstanding.

        Args:
            start: Required inclusive start date in YYYY-MM-DD format.
            end: Required inclusive end date in YYYY-MM-DD format.
        """
        if not start or not start.strip():
            raise ValueError("start is required")
        if not end or not end.strip():
            raise ValueError("end is required")

        from market_data_hub.reader import read_treasury_debt

        frame = read_treasury_debt(
            start=start,
            end=end,
            db_path=self._db_path,
        )
        rows = _records(frame)
        return {
            "start": start,
            "end": end,
            "returned": len(rows),
            "rows": rows,
        }

    def treasury_auctions(
        self,
        start: str,
        end: str,
        security_type: str = "",
    ) -> dict:
        """Read Treasury auction results in a required date range, optionally matching the vendor's exact ``security_type`` vocabulary, such as ``Bill``, ``Note``, ``Bond``, ``TIPS``, or ``FRN``.

        Args:
            start: Required inclusive start date in YYYY-MM-DD format.
            end: Required inclusive end date in YYYY-MM-DD format.
            security_type: Exact security-type label to match, such as 'Bill' or 'Note'; pass an empty string for all security types.
        """
        if not start or not start.strip():
            raise ValueError("start is required")
        if not end or not end.strip():
            raise ValueError("end is required")

        from market_data_hub.reader import read_treasury_auctions

        security_filter = security_type or None
        frame = read_treasury_auctions(
            start=start,
            end=end,
            security_type=security_filter,
            db_path=self._db_path,
        )
        rows = _records(frame)
        return {
            "start": start,
            "end": end,
            "security_type": security_filter,
            "returned": len(rows),
            "rows": rows,
        }

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(
                self.treasury_cash_balance,
                name="treasury_cash_balance",
            ),
            Tool.wrap(self.treasury_debt, name="treasury_debt"),
            Tool.wrap(self.treasury_auctions, name="treasury_auctions"),
        ]
