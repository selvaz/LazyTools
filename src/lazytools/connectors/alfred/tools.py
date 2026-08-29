"""ALFRED point-in-time macro data as LazyBridge tools.

Read-only by construction: FRED publishes, this asks. There is no write
surface to gate, so ``allow_write`` has nothing to mean here.
"""

from __future__ import annotations

from typing import Any

from lazytools.connectors.alfred.client import ALFREDClient

#: Above this, an answer stops being something a model can reason over and
#: becomes a data dump that crowds out its own context.
MAX_OBSERVATIONS = 400


class ALFREDTools:
    """A LazyBridge ``ToolProvider`` over ALFRED, FRED's vintage view.

    The point of this connector, and the reason it is worth a tool of its
    own rather than a flag on a FRED reader: asking FRED for "CPI in March
    2020" today returns the number as revised since, which nobody knew in
    March 2020. A backtest that decides on that number is reading the
    future. Pinning the vintage is what makes the answer honest.
    """

    _is_lazy_tool_provider = True

    def __init__(self, *, client: ALFREDClient | None = None,
                 max_calls: int | None = 200) -> None:
        self._client = client or ALFREDClient(max_calls=max_calls)

    def alfred_vintage(
        self,
        series_id: str,
        as_of: str,
        start: str = "",
        end: str = "",
    ) -> dict:
        """Read a FRED series exactly as it was published on a past date, so a backtest sees what was actually knowable then instead of today's revised figures; pass the decision date as as_of, optionally narrowing which observations come back with start and end.

        Args:
            series_id: Required FRED series identifier, for example ``CPIAUCSL`` or ``UNRATE``.
            as_of: Required vintage date in ``YYYY-MM-DD`` form; the series is returned as it stood on this day. Use ``alfred_vintage_dates`` to find dates that exist.
            start: Optional first observation date in ``YYYY-MM-DD`` form; empty means no lower bound.
            end: Optional last observation date in ``YYYY-MM-DD`` form; empty means no upper bound.
        """
        if not series_id or not series_id.strip():
            raise ValueError("series_id is required")
        if not as_of or not as_of.strip():
            raise ValueError(
                "as_of is required -- without a vintage this is a plain FRED "
                "read, which returns today's revised numbers"
            )
        series_id = series_id.strip()
        rows = self._client.observations(
            series_id, start=start or None, end=end or None, as_of=as_of.strip()
        )
        truncated = len(rows) > MAX_OBSERVATIONS
        # Keep the NEWEST when truncating: a vintage read is almost always
        # asked from the decision date backwards, so the recent end is the
        # end that carries the answer.
        kept = rows[-MAX_OBSERVATIONS:] if truncated else rows
        out: dict[str, Any] = {
            "series_id": series_id,
            "as_of": as_of.strip(),
            "start": start or None,
            "end": end or None,
            "returned": len(kept),
            "observations": [
                {"date": o.date, "value": o.value, "as_of": o.as_of} for o in kept
            ],
            "calls_made": self._client.calls_made,
        }
        if truncated:
            out["truncated"] = (
                f"{len(rows)} observations matched, the newest {len(kept)} are "
                f"returned. Narrow the window with start/end to see the rest."
            )
        if not kept:
            out["note"] = (
                "No observations at this vintage. Either the series did not "
                "exist yet on this date, or the window falls outside it -- "
                "alfred_vintage_dates shows when the series was actually "
                "being published."
            )
        return out

    def alfred_vintage_dates(self, series_id: str, limit: int = 200) -> dict:
        """List the dates on which a FRED series was revised, newest first, so a caller can pick a vintage that really exists rather than guessing one and silently getting an empty answer.

        Args:
            series_id: Required FRED series identifier, for example ``CPIAUCSL``.
            limit: How many vintage dates to return, newest first; the reply reports the vendor's own total so a truncated answer is visible as truncated.
        """
        if not series_id or not series_id.strip():
            raise ValueError("series_id is required")
        series_id = series_id.strip()
        dates, total = self._client.vintage_dates(series_id, limit=max(1, int(limit)))
        out: dict[str, Any] = {
            "series_id": series_id,
            "returned": len(dates),
            "total": total,
            "vintage_dates": dates,
            "calls_made": self._client.calls_made,
        }
        if total > len(dates):
            # Reported rather than silently dropped: a partial list that looks
            # complete is how a caller concludes a series has a short history.
            out["truncated"] = (
                f"the series has {total} vintages, the newest {len(dates)} are "
                f"returned. Raise limit to reach older ones."
            )
        return out

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.alfred_vintage, name="alfred_vintage"),
            Tool.wrap(self.alfred_vintage_dates, name="alfred_vintage_dates"),
        ]
