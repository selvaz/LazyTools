"""Selecting one XBRL fact out of many that all look right.

The SEC's company-concept payload is not a table of answers; it is every
observation a filer ever tagged with one concept, grouped by unit of measure.
Several of them will match a naive query, and picking the first is how a caller
ends up reporting six-month revenue as a quarter.

Three distinctions do the work here, and none of them can be made by looking at
a single field:

* **Duration.** A Q2 10-Q carries the three-month and the year-to-date figure
  under the same concept, the same accession, the same ``end``, and the same
  ``fy``/``fp``. Only ``start`` separates them.
* **Focus is not period.** ``fy``/``fp`` describe the *document* the observation
  appeared in, so a prior-year comparative inside an FY2026 Q2 filing is also
  labelled ``fy=2026, fp=Q2``. They are useful for finding a filing and useless
  for identifying a period.
* **Version.** The same economic period is re-reported across filings —
  restated, recast for discontinued operations, or simply presented again as a
  comparative. "Latest filed" and "as originally reported" are both defensible
  and are different numbers, so :class:`VersionPolicy` makes the caller say
  which one they meant instead of inheriting whichever the sort happened to put
  first.

Ambiguity is returned, never resolved by luck: :func:`select` hands back every
survivor and :func:`pick` reports what it set aside.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from lazytools.connectors.edgar.period import ResolvedWindow

#: How to choose among several filings reporting the same period.
#:
#: ``original``  — the earliest filing that reported it: what the company said
#:                 at the time, the right answer for "what did they report?"
#: ``latest``    — the most recently filed value: the current presentation,
#:                 including restatements and recasts.
#: ``as_of``     — the latest value filed on or before a cutoff date: what a
#:                 reader would have seen then. Reproducible; the other two
#:                 drift as new filings land.
VersionPolicy = Literal["original", "latest", "as_of"]


class AmbiguousFactError(ValueError):
    """Several facts survived selection and no policy separates them.

    Raised rather than returning one: two different numbers both answering the
    question is a finding about the filing, and swallowing it produces a
    confident answer with no way to notice it was a coin flip.
    """

    def __init__(self, message: str, *, candidates: list[Fact]) -> None:
        super().__init__(message)
        self.candidates = candidates


class FactParseError(ValueError):
    """At least one reported observation could not be read.

    Two different losses hide behind a silently shortened list. If EVERY row
    fails, "the company never reported this" and "the payload no longer looks
    the way we parse it" become indistinguishable, and only the first is a
    finding about the company. If SOME rows fail, the missing one may be the
    restatement -- in which case the surviving original is returned as the
    current figure, with nothing anywhere saying a row went missing.

    ``dropped`` carries the offending rows so a caller can show them.
    """

    def __init__(self, message: str, *, seen: int, dropped: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.seen = seen
        self.dropped = dropped or []


@dataclass(frozen=True)
class Fact:
    """One XBRL observation, with everything needed to cite and re-find it.

    ``start`` is ``None`` for an instant (a balance-sheet fact); duration facts
    (income statement, cash flow) always carry both endpoints.
    """

    concept: str
    taxonomy: str
    unit: str
    value: float
    start: date | None
    end: date
    accession: str
    form: str
    filed: date
    fy: int | None = None
    fp: str | None = None
    frame: str | None = None

    @property
    def duration_days(self) -> int | None:
        """Inclusive length in days, or ``None`` for an instant."""
        return None if self.start is None else (self.end - self.start).days + 1

    @property
    def is_amendment(self) -> bool:
        """Filed on an amended form (``10-K/A``, ``10-Q/A``, ``20-F/A``, …).

        An amendment does **not** automatically supersede: a ``/A`` often amends
        an exhibit or Part III and leaves the statements untouched. This only
        reports the form's shape; deciding what it means is the caller's.
        """
        return self.form.upper().endswith("/A")


@dataclass(frozen=True)
class ParseResult:
    """What :func:`parse_concept` could read, and what it could not.

    ``dropped`` is not diagnostics. A dropped row is a reported observation this
    code failed to understand, and the danger is specific: the row a restatement
    arrived on is exactly the one whose loss makes ``pick(policy="latest")``
    return the superseded number with full confidence.
    """

    facts: list[Fact]
    dropped: list[dict[str, Any]]

    @property
    def seen(self) -> int:
        """Observations present in the payload, readable or not."""
        return len(self.facts) + len(self.dropped)


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


def select(
    facts: list[Fact],
    window: ResolvedWindow,
    *,
    unit: str | None = None,
    forms: tuple[str, ...] | None = None,
) -> list[Fact]:
    """Every fact whose own reported period matches ``window``.

    Args:
        facts: from :func:`facts_from_concept`.
        window: from ``period.resolve`` — matching uses both endpoints, which is
            what keeps a year-to-date fact from passing as a quarterly one.
        unit: keep only this unit of measure (e.g. ``"USD"``). Leaving it
            ``None`` keeps every unit, which is rarely what a caller wants but
            is at least visible in the result.
        forms: keep only these forms, compared case-insensitively. An entry
            without ``/A`` does **not** implicitly admit its amended twin — pass
            both when you want both.

    Filtering never happens on ``fy``/``fp``. See the module docstring.
    """
    keep = facts
    if unit is not None:
        keep = [f for f in keep if f.unit == unit]
    if forms is not None:
        wanted = {f.upper() for f in forms}
        keep = [f for f in keep if f.form.upper() in wanted]
    return [f for f in keep if window.accepts(f.start, f.end)]


def pick(
    candidates: list[Fact],
    *,
    policy: VersionPolicy = "latest",
    as_of: date | None = None,
) -> tuple[Fact, list[Fact]]:
    """Apply a version policy; return the chosen fact and what it displaced.

    The second element is every other candidate, newest filing first, so a
    caller can show "we used the restated figure; the originally reported one
    was X" instead of quietly discarding it.

    Args:
        candidates: survivors from :func:`select`.
        policy: see :data:`VersionPolicy`.
        as_of: required by ``policy="as_of"``; ignored otherwise.

    Raises:
        ValueError: on an empty candidate list, or ``as_of`` without a date.
        AmbiguousFactError: when the policy cannot separate the front-runners —
            two different values filed on the same date, which means the filing
            itself reports the period twice and a choice here would be a guess.
    """
    if not candidates:
        raise ValueError("pick() needs at least one candidate fact")
    # Validated rather than defaulted: without this, every value that is not the
    # exact string "original" -- including the typo "orginal" -- takes the
    # latest branch and silently returns a restated figure to a caller who asked
    # for the original one.
    if policy not in ("original", "latest", "as_of"):
        raise ValueError(
            f"unknown version policy {policy!r}; expected 'original', 'latest', or 'as_of'"
        )

    pool = candidates
    if policy == "as_of":
        if as_of is None:
            raise ValueError('policy="as_of" requires an as_of date')
        pool = [f for f in candidates if f.filed <= as_of]
        if not pool:
            raise ValueError(
                f"no candidate was filed on or before {as_of.isoformat()}; "
                f"the earliest was {min(f.filed for f in candidates).isoformat()}"
            )

    reverse = policy != "original"
    ordered = sorted(pool, key=lambda f: (f.filed, f.accession), reverse=reverse)
    chosen = ordered[0]

    # A tie on the deciding key is only a real ambiguity when the values differ:
    # the same number reported twice on one day is a duplicate, not a conflict.
    tied = [f for f in ordered[1:] if f.filed == chosen.filed and f.value != chosen.value]
    if tied:
        raise AmbiguousFactError(
            f"{len(tied) + 1} different values for {chosen.concept} were filed on "
            f"{chosen.filed.isoformat()} ({', '.join(str(f.value) for f in [chosen, *tied])}); "
            "the period is reported more than once and no version policy separates them",
            candidates=[chosen, *tied],
        )
    rest = sorted(
        (f for f in candidates if f is not chosen),
        key=lambda f: (f.filed, f.accession),
        reverse=True,
    )
    return chosen, rest


__all__ = [
    "AmbiguousFactError",
    "Fact",
    "FactParseError",
    "ParseResult",
    "VersionPolicy",
    "facts_from_company_facts",
    "facts_from_concept",
    "parse_concept",
    "pick",
    "select",
]
