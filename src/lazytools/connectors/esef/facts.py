"""Reading an ESEF filing's xBRL-JSON into facts.

The European branch of the retriever does not need statement rendering: an ESEF
filing publishes its tagged data as xBRL-JSON, so the figures arrive structured.
What they do not arrive with is any of the context a reader assumes, and every
assumption here was measured against TotalEnergies' 2024 filing on 2026-08-29.

**Both period endpoints are exclusive, instants included.** A duration written
``2024-01-01T00:00:00/2025-01-01T00:00:00`` is the year ending 31 December 2024,
and an instant written ``2025-01-01T00:00:00`` is the balance AT 31 December
2024 — not at 1 January 2025. That is the Open Information Model's convention
and it is easy to read straight past: TotalEnergies' cash of $25.8bn is stamped
2025-01-01, so taking the date literally files the year-end balance under the
wrong year, and every year-on-year comparison shifts by one.

**A concept may be the issuer's own.** Alongside ``ifrs-full:`` the filing
carries ``tot:`` — TotalEnergies' extension namespace, declared in
``documentInfo.namespaces``. An extension is not a defect; IFRS lets an issuer
tag what the standard taxonomy does not name. But it means a consumer cannot
work from the standard taxonomy alone, and an extension concept means whatever
the issuer decided it means.

**A dimensioned fact is a slice, not the total.** The only axis in that filing
is ``ifrs-full:ComponentsOfEquityAxis``, but the rule holds generally: a fact
carrying any axis beyond the identity dimensions describes part of the figure.
Summing slices double-counts and taking one reports a component as the whole,
so they are marked and left for the caller to exclude.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from lazytools.financials.facts import Fact, FactParseError, ParseResult

#: Dimensions that identify a fact rather than slice it. Anything else is an
#: axis, and a fact carrying one is a part of its concept's total.
IDENTITY_DIMENSIONS = frozenset({"concept", "entity", "period", "unit", "language", "noteId"})

#: The unit prefix for a currency amount. Facts in shares or pure ratios keep
#: their own unit string rather than being coerced into money.
_CURRENCY_PREFIX = "iso4217:"


def parse_facts(
    document: dict[str, Any], *, filing_id: str, form: str = "ESEF"
) -> ParseResult:
    """Every numeric fact in an xBRL-JSON document.

    Args:
        document: the parsed xBRL-JSON, as :meth:`ESEFClient.facts_json` returns.
        filing_id: how to cite this filing. ESEF has no accession number, so the
            repository's own path for the filing is used.
        form: recorded on each fact.

    Non-numeric facts — the legal form of the entity, an explanation of a name
    change — are skipped rather than dropped with an error: they are text, and
    text was never a figure that went missing.

    Raises:
        FactParseError: when facts exist but none could be read, or when some
            could not. A silently shortened list hides two different losses, and
            only one of them is a fact about the company.
    """
    raw = document.get("facts")
    if not isinstance(raw, dict):
        raise FactParseError("no facts object in the document", seen=0)

    facts: list[Fact] = []
    dropped: list[dict[str, Any]] = []
    numeric_seen = 0
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        dimensions = entry.get("dimensions")
        if not isinstance(dimensions, dict) or "unit" not in dimensions:
            continue                      # text, not a figure
        numeric_seen += 1
        fact = _to_fact(entry, dimensions, filing_id=filing_id, form=form)
        if fact is None:
            dropped.append(entry)
        else:
            facts.append(fact)

    if numeric_seen and not facts:
        raise FactParseError(
            f"{numeric_seen} numeric facts and none could be read; the payload's "
            "shape has probably changed", seen=numeric_seen, dropped=dropped)
    return ParseResult(facts=facts, dropped=dropped)


def is_dimensioned(dimensions: dict[str, Any]) -> bool:
    """Whether this fact is a slice of its concept rather than the whole.

    A consolidated figure is the one carrying no axis. Nothing here excludes
    dimensioned facts — a caller asking about a segment wants exactly them — but
    a caller asking for the group total must not sum across them.
    """
    return bool(set(dimensions) - IDENTITY_DIMENSIONS)


def extension_namespaces(document: dict[str, Any]) -> tuple[str, ...]:
    """Namespace prefixes the issuer defined for itself.

    Everything outside the standard taxonomies and the XBRL infrastructure. A
    concept in one of these means what the issuer decided it means, which is
    worth knowing before treating it as comparable to anything.
    """
    declared = (document.get("documentInfo") or {}).get("namespaces") or {}
    known = ("ifrs", "iso4217", "scheme", "xbrl", "xbrli", "utr", "link", "xlink", "esef")
    return tuple(sorted(
        prefix for prefix in declared
        if not any(prefix.lower().startswith(word) for word in known)))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _to_fact(
    entry: dict[str, Any], dimensions: dict[str, Any], *, filing_id: str, form: str
) -> Fact | None:
    concept = dimensions.get("concept")
    period = dimensions.get("period")
    if not isinstance(concept, str) or not isinstance(period, str):
        return None
    window = _window(period)
    if window is None:
        return None
    try:
        value = float(entry["value"])
    except (KeyError, TypeError, ValueError):
        return None

    start, end = window
    taxonomy, _, name = concept.partition(":")
    unit = str(dimensions.get("unit", ""))
    return Fact(
        concept=name or concept,
        taxonomy=taxonomy if name else "",
        unit=unit.removeprefix(_CURRENCY_PREFIX),
        value=value,
        start=start,
        end=end,
        accession=filing_id,
        form=form,
        # An ESEF repository dates a report by the period it covers and by when
        # it was ingested. Neither is a filing date, and substituting one would
        # make a version policy's ordering look decided when it is not.
        filed=None,
    )


def _window(period: str) -> tuple[date | None, date] | None:
    """``(start, end)`` from an xBRL-JSON period, with both ends made inclusive.

    The exclusive endpoint is the whole reason this function exists. A duration
    ending ``2025-01-01T00:00:00`` ends on 31 December 2024, and an instant
    stamped the same way is the balance at that same 31 December.
    """
    head, separator, tail = period.partition("/")
    if separator:
        start, end = _moment(head), _moment(tail)
        if start is None or end is None:
            return None
        return start, end - timedelta(days=1)
    instant = _moment(head)
    return None if instant is None else (None, instant - timedelta(days=1))


def _moment(text: str) -> date | None:
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


__all__ = [
    "IDENTITY_DIMENSIONS",
    "extension_namespaces",
    "is_dimensioned",
    "parse_facts",
]
