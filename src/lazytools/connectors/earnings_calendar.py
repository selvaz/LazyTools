"""Read tools over market-data-hub's corporate earnings calendar.

What is coming this week, what came out, and how it groups by country, sector
or theme. Every window is served from what the hub stored, so a past week
answers exactly as well as a future one -- the upstream source keeps only the
last and next release per company, and no history at all.

Read-only throughout: nothing here writes to the hub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_HUB_MISSING = (
    "market-data-hub is not importable. The earnings tools read from it "
    "directly; install it or put it on the path."
)


class EarningsCalendarTools:
    """A LazyBridge ``ToolProvider`` over the hub's earnings calendar.

    Like its ``EconCalendarTools`` sibling, the methods restate their arguments
    rather than wrapping the hub's functions directly: those take an open
    connection as their first parameter, which a model cannot supply, and use
    ``Optional[float]`` for absence, which a tool schema expresses poorly. Each
    method forwards to exactly one hub function and adds no behaviour.
    """

    _is_lazy_tool_provider = True

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    def _con(self) -> Any:
        try:
            from market_data_hub.db import connection as cx
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(_HUB_MISSING) from exc
        # Absolute, resolved here: the hub reads a relative db_path against its
        # own repository root, which would silently open an empty database.
        percorso = None
        if self._db_path:
            percorso = str(Path(self._db_path).expanduser().resolve())
        return cx.get_conn(percorso, read_only=True)

    # ------------------------------------------------------------------ tools
    def earnings_vocabulary(self) -> dict:
        """The values the other earnings tools will actually match, with counts.

        Call this first. Regions are a closed vocabulary and themes are curated,
        so a guessed value returns an empty result indistinguishable from a
        genuine "nothing that week". Also reports the window actually stored,
        which is the real limit on how far back a question can reach.
        """
        from market_data_hub.earnings_calendar import vocabulary

        con = self._con()
        try:
            return vocabulary(con)
        finally:
            con.close()

    def earnings_week(
        self,
        start: str,
        end: str,
        region: str = "",
        sector: str = "",
        theme: str = "",
        status: str = "",
        min_market_cap: float = 0,
        limit: int = 200,
    ) -> list[dict]:
        """The releases expected or published in a window, largest first.

        Args:
            start: 'YYYY-MM-DD', inclusive.
            end: 'YYYY-MM-DD', exclusive.
            region: one of the regions from earnings_vocabulary().
            sector: the source's own sector name, e.g. 'Electronic Technology'.
            theme: a curated theme, e.g. 'ai_semis'.
            status: 'estimated', 'confirmed' or 'occurred'.
            min_market_cap: in USD; 0 for no floor.
            limit: rows returned, largest capitalisation first.

        Market cap is comparable across countries. EPS and revenue are in each
        instrument's own currency, so compare those only against their own
        estimate, never between companies.
        """
        from market_data_hub.earnings_calendar import events_between

        con = self._con()
        try:
            return events_between(
                con, start, end,
                region=region or None, sector=sector or None,
                theme=theme or None, status=status or None,
                min_market_cap=min_market_cap or None, limit=limit,
            )
        finally:
            con.close()

    def earnings_for_day(self, day: str, region: str = "",
                         min_market_cap: float = 0, limit: int = 200) -> list[dict]:
        """The releases of a single day, largest first.

        Args:
            day: 'YYYY-MM-DD'. The window is that UTC day.
            region: one of the regions from earnings_vocabulary().
            min_market_cap: in USD; 0 for no floor.
            limit: rows returned.
        """
        from datetime import date, timedelta

        from market_data_hub.earnings_calendar import events_between

        giorno = date.fromisoformat(day)
        con = self._con()
        try:
            return events_between(
                con, str(giorno), str(giorno + timedelta(days=1)),
                region=region or None, min_market_cap=min_market_cap or None,
                limit=limit,
            )
        finally:
            con.close()

    def earnings_aggregate(self, start: str, end: str, by: str = "country",
                           min_market_cap: float = 0) -> list[dict]:
        """How a window's releases distribute, without listing them.

        This is what makes a crowded week readable: a Chinese reporting
        deadline is 165 releases, which is a line of summary rather than 165
        rows to read.

        Args:
            start: 'YYYY-MM-DD', inclusive.
            end: 'YYYY-MM-DD', exclusive.
            by: 'country', 'region', 'sector', 'industry' or 'theme'.
            min_market_cap: in USD; 0 for no floor.

        Each bucket carries how many releases fall in it, their combined
        capitalisation, and how many have already happened.
        """
        from market_data_hub.earnings_calendar import aggregate

        con = self._con()
        try:
            return aggregate(con, start, end, by=by,
                             min_market_cap=min_market_cap or None)
        finally:
            con.close()

    def earnings_event(self, event_id: str) -> dict:
        """One release with every version the hub recorded for it.

        The versions are where a moved date is visible: the event itself only
        carries the current answer. Empty when the id is unknown.
        """
        from market_data_hub.earnings_calendar import event_history

        con = self._con()
        try:
            return event_history(con, event_id)
        finally:
            con.close()

    # ---------------------------------------------------------------- wiring
    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.earnings_vocabulary, name="earnings_vocabulary"),
            Tool.wrap(self.earnings_week, name="earnings_week"),
            Tool.wrap(self.earnings_for_day, name="earnings_for_day"),
            Tool.wrap(self.earnings_aggregate, name="earnings_aggregate"),
            Tool.wrap(self.earnings_event, name="earnings_event"),
        ]
