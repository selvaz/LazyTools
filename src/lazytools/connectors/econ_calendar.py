"""Read tools over market-data-hub's economic release calendar.

Four questions an agent actually has: what does this calendar cover, what does
this indicator mean, what came out, and by how much did it miss.

**The economic knowledge is read, not written here.** The catalogue already
carries a description and a methodology note per archetype and a rationale per
indicator -- why a y/y CPI matters *in that country*, that the Japanese core
excludes only fresh food, that Chinese fixed asset investment is published
cumulatively and has to be differenced. Restating any of it in a system prompt
would create a second copy that drifts from the first the moment somebody edits
the catalogue, and the copy in the prompt is the one nobody would update.

So these tools hand the catalogue's own words to the agent, and the agent's
instructions teach it how to *use* them rather than what they say.

Read-only throughout: nothing here writes to the hub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_HUB_MISSING = (
    "market-data-hub is not importable. The calendar tools read from it "
    "directly; install it or put it on the path."
)


class EconCalendarTools:
    """A LazyBridge ``ToolProvider`` over the hub's economic calendar.

    **Why these methods restate their arguments instead of wrapping the hub's
    functions directly.** ``RegimeTools`` is the house pattern and it hands
    ``Tool.wrap`` the library's own bound methods, adding no signature of its
    own; the rule that comes with it is not to write bridge classes that
    re-declare parameter lists, because two copies of a signature drift.

    Two things stop that working here, and both are about what an LLM can
    actually call rather than about taste:

    - ``available_series(con, ...)`` takes an open connection as its first
      parameter. A model cannot supply one, and a partial that hides it loses
      the signature the tool schema is generated from.
    - the hub's parameters are typed for Python — ``Optional[Iterable[str]]``
      for tags, ``None`` for absent. A tool schema expresses neither well: a
      model omitting an optional argument and a model passing null are not
      reliably distinguishable downstream. So tags arrive as ``"a,b"`` and
      absence as ``""``, and this class is where that translation lives.

    It is a translation layer, then, not a second API: every method forwards
    to exactly one hub function and adds no behaviour. If the hub ever grows
    an entry point that binds a connection and speaks in LLM-shaped types,
    these should collapse into direct wraps and this note should go with them.
    """

    _is_lazy_tool_provider = True

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    def _con(self) -> Any:
        try:
            from market_data_hub.db import connection as cx
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(_HUB_MISSING) from exc
        # Resolved here, absolute, on purpose. The hub reads a *relative*
        # db_path as relative to its own repository root, which is right for a
        # writer living inside it and wrong for a reader called from anywhere
        # else: a caller passing 'calendar.duckdb' from its own directory got a
        # brand new empty database created inside market-data-hub, and every
        # tool then answered 'nothing found' about a catalogue that was fine.
        percorso = None
        if self._db_path:
            percorso = str(Path(self._db_path).expanduser().resolve())
        return cx.get_conn(percorso, read_only=True)

    # ------------------------------------------------------------------ tools
    def calendar_vocabulary(self) -> dict:
        """The values the other calendar tools will actually match, with counts.

        Call this first. The filters are a closed vocabulary -- twelve
        categories, two data types, three criticality tiers -- and guessing a
        value that does not exist returns an empty result indistinguishable
        from a genuine 'nothing matched'.
        """
        from market_data_hub.econ_calendar.catalog import catalogue_vocabulary

        con = self._con()
        try:
            return catalogue_vocabulary(con)
        finally:
            con.close()

    def calendar_list_series(
        self,
        country: str = "",
        category: str = "",
        criticality: str = "",
        data_type: str = "",
        tags: str = "",
        from_day: str = "",
        to_day: str = "",
        released_only: bool = False,
    ) -> list[dict]:
        """Which series the calendar covers, filtered along any combination.

        Args:
            country: ISO3, e.g. 'USA', 'IND', 'EMU' for the aggregate euro area.
            category: one of the categories from calendar_vocabulary().
            criticality: 'T1' critical, 'T2' notable, 'T3' context.
            data_type: 'hard' for measured figures, 'soft' for surveys.
            tags: comma separated, e.g. 'flash_final,revision_prone'.
            from_day: 'YYYY-MM-DD'; with to_day, restricts to that window.
            to_day: 'YYYY-MM-DD'.
            released_only: only releases that have actually printed.

        Each row carries `events` (how many releases fall in the window) and
        `with_reference_date` (how many of those carry a reference period).
        A series with events=0 is one the calendar tracks that had nothing in
        the window -- which is a different answer from one it does not track.
        """
        from market_data_hub.econ_calendar.catalog import available_series

        con = self._con()
        try:
            return available_series(
                con,
                country=country or None,
                category=category or None,
                criticality=criticality or None,
                data_type=data_type or None,
                tags=[t.strip() for t in tags.split(",") if t.strip()] or None,
                from_day=from_day or None,
                to_day=to_day or None,
                released_only=released_only,
            )
        finally:
            con.close()

    def calendar_explain_indicator(self, indicator_key: str) -> dict:
        """What an indicator is, how it is built, and why it matters there.

        The description and methodology are the catalogue's own, shared across
        every country that publishes the same kind of figure; the rationale is
        specific to this country. `tags` change how the number must be read --
        see the agent's instructions.
        """
        con = self._con()
        try:
            r = con.execute(
                """
                SELECT indicator_key, name, area, country_iso3, category,
                       data_type, side, tags, frequency, criticality, nature,
                       agency, description, methodology, rationale,
                       macro_indicator_id
                FROM calendar_indicators WHERE lower(indicator_key) = lower(?)
                """,
                [indicator_key],
            ).fetchone()
            if r is None:
                return {"error": f"no indicator {indicator_key!r}; "
                                 "use calendar_list_series to find its key"}
            return dict(zip(
                ("indicator_key", "name", "area", "country_iso3", "category",
                 "data_type", "side", "tags", "frequency", "criticality",
                 "nature", "agency", "description", "methodology", "rationale",
                 "macro_indicator_id"), r, strict=True))
        finally:
            con.close()

    def calendar_recent_releases(
        self,
        from_day: str,
        to_day: str = "",
        country: str = "",
        criticality: str = "",
    ) -> list[dict]:
        """What printed in a window, with the expectation and the surprise.

        `surprise` is null wherever the sources disagreed on what was expected;
        `consensus_low`/`consensus_high` give the range they gave instead. A
        null surprise is not a missing number, it is the honest answer.
        """
        dove = ["e.status = 'released'", "e.release_utc >= ?::date"]
        parametri: list[Any] = [from_day]
        if to_day:
            dove.append("e.release_utc < ?::date + INTERVAL 1 DAY")
            parametri.append(to_day)
        if country:
            dove.append("lower(i.country_iso3) = lower(?)")
            parametri.append(country)
        if criticality:
            dove.append("lower(i.criticality) = lower(?)")
            parametri.append(criticality)

        con = self._con()
        try:
            righe = con.execute(f"""
                SELECT i.indicator_key, i.name, i.area, i.criticality, i.category,
                       strftime(e.release_utc, '%Y-%m-%d %H:%M') AS released_utc,
                       e.release_precision, e.reference_period, e.reference_date,
                       e.reference_date_origin,
                       e.actual, e.consensus, e.previous,
                       CASE WHEN NOT e.consensus_disputed
                            THEN e.actual_num - e.consensus_num END AS surprise,
                       e.consensus_disputed, e.consensus_low, e.consensus_high,
                       e.n_sources, e.values_agree, i.tags
                FROM calendar_events e
                JOIN calendar_indicators i ON i.indicator_key = e.indicator_key
                WHERE {' AND '.join(dove)}
                ORDER BY i.criticality, e.release_utc
            """, parametri).fetchall()
            return [dict(zip(
                ("indicator_key", "name", "area", "criticality", "category",
                 "released_utc", "release_precision", "reference_period",
                 "reference_date", "reference_date_origin", "actual",
                 "consensus", "previous", "surprise", "consensus_disputed",
                 "consensus_low", "consensus_high", "n_sources", "values_agree",
                 "tags"), r, strict=True)) for r in righe]
        finally:
            con.close()

    # ---------------------------------------------------------------- wiring
    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.calendar_vocabulary, name="calendar_vocabulary"),
            Tool.wrap(self.calendar_list_series, name="calendar_list_series"),
            Tool.wrap(self.calendar_explain_indicator,
                      name="calendar_explain_indicator"),
            Tool.wrap(self.calendar_recent_releases,
                      name="calendar_recent_releases"),
        ]
