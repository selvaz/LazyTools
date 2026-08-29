"""Reading SEC company-concept and companyfacts payloads into facts.

The selection rules are not SEC-specific and live in
:mod:`lazytools.financials.facts`. What IS specific is the shape the SEC's
XBRL APIs serve: observations grouped by unit of measure, each carrying the
filing's own accession, form and filed date.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from lazytools.financials.facts import Fact, FactParseError, ParseResult


def parse_concept(payload: dict[str, Any]) -> ParseResult:
    """Flatten a company-concept payload, keeping what failed alongside what did not.

    Every unit of measure is kept and carried on each fact. Dropping to a single
    unit here would be a silent decision: a filer can report the same concept in
    more than one currency, and EPS arrives as ``USD-per-shares`` while a ratio
    arrives as ``pure``.

    This never raises on malformed rows — it reports them. :func:`facts_from_concept`
    is the strict wrapper for callers that want a plain list.
    """
    concept = str(payload.get("tag", ""))
    taxonomy = str(payload.get("taxonomy", ""))
    facts: list[Fact] = []
    dropped: list[dict[str, Any]] = []
    for unit, observations in (payload.get("units") or {}).items():
        for row in observations or []:
            fact = _fact_from_row(row, concept=concept, taxonomy=taxonomy, unit=str(unit))
            if fact is None:
                dropped.append(dict(row, unit=str(unit)))
            else:
                facts.append(fact)
    return ParseResult(facts=facts, dropped=dropped)


def facts_from_concept(payload: dict[str, Any], *, strict: bool = True) -> list[Fact]:
    """The readable observations, refusing by default to lose any silently.

    Args:
        strict: when ``True`` (the default) **any** unreadable observation
            raises :class:`FactParseError`, not merely a payload where all of
            them failed. Tolerating partial loss is what lets a malformed
            restatement disappear while the original still parses, so
            ``pick(policy="latest")`` returns the superseded figure and nothing
            anywhere says a row went missing. Pass ``strict=False`` — or use
            :func:`parse_concept` and inspect ``dropped`` — when you would
            rather see the rest.

    Raises:
        FactParseError: when ``strict`` and any observation could not be read.
            The exception carries ``dropped`` so a caller can show what was lost.
    """
    result = parse_concept(payload)
    if strict and result.dropped:
        concept = str(payload.get("tag", ""))
        taxonomy = str(payload.get("taxonomy", ""))
        raise FactParseError(
            f"{len(result.dropped)} of {result.seen} observation(s) for "
            f"{taxonomy}:{concept} could not be read; a dropped row may be the "
            "one carrying a restatement, so this is reported rather than "
            "silently skipped (pass strict=False to take the rest)",
            seen=result.seen,
            dropped=result.dropped,
        )
    return result.facts


def facts_from_company_facts(
    payload: dict[str, Any], *, taxonomy: str, tag: str
) -> list[Fact]:
    """The same flattening, for one concept inside a full companyfacts payload.

    Useful when a caller already holds companyfacts and does not want to spend a
    second request to read one concept out of it.
    """
    body = (payload.get("facts", {}).get(taxonomy) or {}).get(tag)
    if body is None:
        return []
    return facts_from_concept({"tag": tag, "taxonomy": taxonomy, "units": body.get("units", {})})


def _fact_from_row(
    row: dict[str, Any], *, concept: str, taxonomy: str, unit: str
) -> Fact | None:
    end = _date(row.get("end"))
    filed = _date(row.get("filed"))
    if end is None or filed is None:
        return None
    # ONLY an absent key is an instant. A start that is present but null, empty,
    # or unparseable is a duration fact we failed to read, and admitting it as an
    # instant is the worst outcome available: `ResolvedWindow.accepts` waves
    # instants through on the end date alone, so a six-month row with a broken
    # start would pass as the quarter it merely shares an end date with.
    start: date | None = None
    if "start" in row:
        start = _date(row["start"])
        if start is None:
            return None
    value = row.get("val")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return Fact(
        concept=concept,
        taxonomy=taxonomy,
        unit=unit,
        value=float(value),
        start=start,
        end=end,
        accession=str(row.get("accn", "")),
        form=str(row.get("form", "")),
        filed=filed,
        fy=row.get("fy") if isinstance(row.get("fy"), int) else None,
        fp=str(row["fp"]) if row.get("fp") else None,
        frame=str(row["frame"]) if row.get("frame") else None,
    )


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


__all__ = ["facts_from_company_facts", "facts_from_concept", "parse_concept"]
