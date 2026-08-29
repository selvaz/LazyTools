"""Read-only tools over market-data-hub's stored ALFRED vintage data.

ALFRED is FRED's real-time/vintage view: it reports what a series said as of
a historical publication date, rather than silently substituting today's
revised value. The hub owns downloading and storing that data; this connector
only translates LLM-shaped arguments into a hub reader call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd


def _records(frame: pd.DataFrame) -> list[dict]:
    """A hub reader's DataFrame as plain, JSON-safe dicts.

    ``DataFrame.to_dict(orient="records")`` alone is not enough: the hub's
    ``read_alfred_vintage`` returns real ``date``/``as_of`` ``Timestamp``
    columns and ``NaN`` for legitimately-absent ``value`` figures -- neither
    is JSON-serializable as-is. Dates become ISO ``YYYY-MM-DD`` strings;
    every other null becomes ``None``. Same fix as the sibling
    ``treasury_fiscal.py``/``cftc_cot.py`` connectors' ``_records`` helper.

    ``pandas`` is imported here, not at module level: this module must stay
    importable (e.g. by the MCP surface-contract test) even where
    market-data-hub -- and therefore pandas -- isn't installed, matching
    the local ``from market_data_hub.reader import ...`` import below.
    """
    import pandas as pd

    frame = frame.copy()
    for column in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            frame[column] = frame[column].dt.strftime("%Y-%m-%d")
    frame = frame.astype(object).where(pd.notnull(frame), None)
    return frame.to_dict(orient="records")


class ALFREDTools:
    """A LazyBridge ``ToolProvider`` over the hub's ALFRED vintage reader.

    **Why this method restates its arguments instead of wrapping the hub
    function directly.** The hub API is shaped for Python callers and accepts
    ``None`` for absent filters. An LLM tool schema cannot reliably distinguish
    an omitted optional argument from null, so this provider exposes strings
    and translates ``""`` to ``None``. It also binds ``db_path`` because a
    model cannot supply the hub's storage configuration. This is a thin
    translation layer: the hub's ingestion job writes and backfills the data,
    while this provider is read-only and never calls FRED itself.
    """

    _is_lazy_tool_provider = True

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    def alfred_vintage(
        self,
        series_id: str,
        date: str = "",
        as_of: str = "",
    ) -> dict:
        """Read ALFRED values as historically published, avoiding revised-data leakage in backtests: give only date for every vintage of one observation, only as_of for every observation known on one vintage date, both for one exact observation/vintage, or neither for everything stored for the series. Results cover only vintages the hub ingestion job has already backfilled; an empty result for a real series usually means it is not yet in ``alfred_vintage_observations``, not that it has no history.

        Args:
            series_id: Required FRED series identifier, for example ``CPIAUCSL``.
            date: Optional observation date in ``YYYY-MM-DD`` form; ``""`` means no observation-date filter.
            as_of: Optional vintage/realtime date in ``YYYY-MM-DD`` form; ``""`` means no vintage-date filter.
        """
        if not series_id or not series_id.strip():
            raise ValueError("series_id is required")

        from market_data_hub.reader import read_alfred_vintage

        series_id = series_id.strip()
        date_filter = date or None
        as_of_filter = as_of or None
        frame = read_alfred_vintage(
            series_id,
            date=date_filter,
            as_of=as_of_filter,
            db_path=self._db_path,
        )
        observations = _records(frame)
        result = {
            "series_id": series_id,
            "date": date_filter,
            "as_of": as_of_filter,
            "returned": len(observations),
            "observations": observations,
        }
        if not observations:
            if date_filter or as_of_filter:
                result["note"] = (
                    "No stored observations matched. This may mean the series has no "
                    "vintage on this exact date/as_of combination, or that the hub "
                    "ingestion job has not backfilled this series at all yet -- retry "
                    "without date/as_of to check whether anything is stored for it."
                )
            else:
                result["note"] = (
                    "No stored observations matched. For a real series, this most likely "
                    "means the hub ingestion job has not backfilled it into "
                    "alfred_vintage_observations yet, not that the series has no history."
                )
        return result

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.alfred_vintage, name="alfred_vintage"),
        ]
