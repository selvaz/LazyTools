"""Deterministic reading of a period phrase — every plausible one, never just one.

"Microsoft Q2 2026" does not identify a period.  Microsoft's fiscal year ends
30 June, so its **fiscal** Q2 FY2026 ran Oct-Dec 2025, while **calendar** Q2
2026 ran Apr-Jun 2026 and was its fiscal Q4.  Both readings are legitimate and
a caller that silently picks one is wrong half the time without ever saying so.

So :func:`interpret` returns a *list*.  Narrowing it is the job of evidence the
issuer itself filed (``dei:DocumentFiscalPeriodFocus`` and friends), not of a
guess made here.

Everything in this module is pure: no network, no clock beyond an explicitly
passed ``today``, no LLM.  That is deliberate — the fiscal/calendar distinction
is arithmetic, and arithmetic that lives in a prompt cannot be tested.

The windows computed for a fiscal period are **targets, not truth**.  A 52/53-week
filer's quarters do not land on month ends, and a filer may change its year end.
Use :attr:`ResolvedWindow.tolerance_days` when matching facts, and treat the
issuer's own ``DocumentPeriodEndDate`` as authoritative when it is available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

PeriodKind = Literal["quarter", "half", "nine_months", "annual"]
Basis = Literal["fiscal", "calendar"]

#: Nominal length of each period kind, in days, with the slack a real filer's
#: calendar needs.  A "quarter" is 13 weeks (91 days) for a 52/53-week filer and
#: 90-92 for a month-end filer; the SEC's own frames API uses 91 +/- 30 for
#: quarters and 365 +/- 30 for years, and we match that rather than inventing a
#: second convention.
_NOMINAL_DAYS: dict[PeriodKind, int] = {
    "quarter": 91,
    "half": 182,
    "nine_months": 273,
    "annual": 365,
}
#: How far each endpoint may drift before a fact stops being this period.
#:
#: Deliberately tighter than the SEC frames convention's +/- 30 days. Frames
#: applies its slack to a period's DURATION; applying 30 days to each endpoint
#: independently widens the window until genuinely different periods fit inside
#: it -- a two-month fact ending on the right date lands exactly 30 days off at
#: the start, and would be accepted as a quarter. A 52/53-week filer's quarter
#: drifts at most about a week from month ends, so 15 covers every real calendar
#: while closing that door.
_TOLERANCE_DAYS = 15

#: Longest each month gets to be, so a year end can be checked against its own
#: month. February is 29 because 29 February is a real year end for some filers.
_DAYS_IN_MONTH: dict[int, int] = {
    1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}

#: Months spanned, used to walk a fiscal year's start forward to a sub-period.
_MONTHS_IN: dict[PeriodKind, int] = {
    "quarter": 3,
    "half": 6,
    "nine_months": 9,
    "annual": 12,
}


class PeriodParseError(ValueError):
    """The phrase carries no period this module can read.

    Raised rather than returning an empty list: "no interpretation" and "the
    caller never asked for a period" are different states, and a silent empty
    list makes them indistinguishable at the call site.
    """


@dataclass(frozen=True)
class PeriodInterpretation:
    """One reading of a period phrase, before any issuer evidence is applied.

    ``ordinal`` counts sub-periods within the year (1-4 for a quarter, 1-2 for
    a half) and is ``None`` for a period that is the whole year.
    """

    basis: Basis
    kind: PeriodKind
    year: int
    ordinal: int | None
    label: str

    @property
    def is_ambiguous_with_calendar(self) -> bool:
        """True for a fiscal reading that a calendar reading also fits."""
        return self.basis == "fiscal"


@dataclass(frozen=True)
class ResolvedWindow:
    """A concrete date window for one interpretation.

    ``start``/``end`` are inclusive.  ``tolerance_days`` is how far a real
    filing's own dates may sit from these without contradicting the reading —
    see the module docstring on why an exact match is the wrong test.
    """

    interpretation: PeriodInterpretation
    start: date
    end: date
    tolerance_days: int = _TOLERANCE_DAYS

    @property
    def duration_days(self) -> int:
        """Inclusive length of the window."""
        return (self.end - self.start).days + 1

    def accepts(self, start: date | None, end: date) -> bool:
        """Does a fact's own reported window fall inside this reading?

        Three checks, and all three are load-bearing:

        * **End date.** The cheap one, and on its own the source of the bug this
          module exists to prevent.
        * **Start date.** A Q2 10-Q's year-to-date fact and its three-month fact
          share an end date, an accession, and a fiscal-period focus, and differ
          *only* in where they start.
        * **Duration.** Two endpoint checks alone still leave a window wider than
          the period: a fact whose start sits at the tolerance limit is a
          different length from the one asked for, and would otherwise be
          accepted on the strength of two near-misses.

        ``start=None`` means a genuine instant — a balance-sheet fact, which has
        no duration to compare. A duration fact whose start could not be read
        must not arrive here as ``None``; see ``facts._fact_from_row``.
        """
        if abs((end - self.end).days) > self.tolerance_days:
            return False
        if start is None:
            return True
        if abs((start - self.start).days) > self.tolerance_days:
            return False
        reported = (end - start).days + 1
        return abs(reported - self.duration_days) <= self.tolerance_days


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_YEAR = r"(?:FY\s*)?((?:19|20)\d{2})"
_PATTERNS: tuple[tuple[re.Pattern[str], PeriodKind, int | None], ...] = (
    (re.compile(rf"\bQ([1-4])\s*[-/ ]?\s*{_YEAR}\b", re.I), "quarter", None),
    (re.compile(rf"\b{_YEAR}\s*[-/ ]?\s*Q([1-4])\b", re.I), "quarter", -1),
    (re.compile(rf"\bH([12])\s*[-/ ]?\s*{_YEAR}\b", re.I), "half", None),
    (re.compile(rf"\b{_YEAR}\s*[-/ ]?\s*H([12])\b", re.I), "half", -1),
    (re.compile(rf"\b(?:9M|nine[- ]months?)\s*[-/ ]?\s*{_YEAR}\b", re.I), "nine_months", 0),
    (re.compile(rf"\b{_YEAR}\s*[-/ ]?\s*(?:9M|nine[- ]months?)\b", re.I), "nine_months", 0),
    (re.compile(rf"\b(?:FY|full[- ]year|annual|fiscal[- ]year)\s*[-/ ]?\s*{_YEAR}\b", re.I), "annual", 0),
    (re.compile(rf"\b{_YEAR}\s+(?:full[- ]year|annual)\b", re.I), "annual", 0),
)
#: A bare year, tried only after every dated pattern above has failed.
_BARE_YEAR = re.compile(rf"\b{_YEAR}\b")


def interpret(phrase: str) -> list[PeriodInterpretation]:
    """Every plausible reading of ``phrase``, most likely first.

    A quarter or half is returned twice — once read as the issuer's fiscal
    period and once as a calendar period — because the phrase alone cannot
    distinguish them.  The fiscal reading comes first: "Q2 2026" in an earnings
    context far more often means the issuer's own second quarter.

    An annual period is returned twice for the same reason ("FY2026" is the
    issuer's year; "2026" may mean the calendar year), and a nine-month period
    only makes sense against a fiscal year, so it is returned once.

    Raises:
        PeriodParseError: when no year can be read from the phrase at all.
    """
    text = phrase.strip()
    if not text:
        raise PeriodParseError("period phrase is empty")

    for pattern, kind, swap in _PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        groups = match.groups()
        if kind in ("annual", "nine_months"):
            year, ordinal = int(groups[0]), None
        elif swap == -1:
            year, ordinal = int(groups[0]), int(groups[1])
        else:
            ordinal, year = int(groups[0]), int(groups[1])
        return _both_bases(kind, year, ordinal)

    match = _BARE_YEAR.search(text)
    if match is not None:
        return _both_bases("annual", int(match.group(1)), None)

    raise PeriodParseError(
        f"no period found in {phrase!r}; expected something like 'Q2 2026', "
        "'H1 2025', 'FY2026', or a bare year"
    )


def _both_bases(kind: PeriodKind, year: int, ordinal: int | None) -> list[PeriodInterpretation]:
    """The fiscal and calendar readings of one parsed period, fiscal first.

    Every kind gets both, nine months included. Reading "9M 2025" as fiscal-only
    looks reasonable until the filer is a non-December one: a caller who meant
    January-September then receives the issuer's own nine months, which is a
    different period and a different number, with nothing in the result saying
    so. Offering both readings costs one extra candidate and lets evidence
    decide.
    """
    name = _label(kind, year, ordinal)
    fiscal = PeriodInterpretation(basis="fiscal", kind=kind, year=year, ordinal=ordinal, label=f"{name} (issuer fiscal)")
    calendar = PeriodInterpretation(
        basis="calendar", kind=kind, year=year, ordinal=ordinal, label=f"{name} (calendar)"
    )
    return [fiscal, calendar]


def _label(kind: PeriodKind, year: int, ordinal: int | None) -> str:
    if kind == "quarter":
        return f"Q{ordinal} {year}"
    if kind == "half":
        return f"H{ordinal} {year}"
    if kind == "nine_months":
        return f"9M {year}"
    return f"FY{year}"


# --------------------------------------------------------------------------- #
# Resolution to dates
# --------------------------------------------------------------------------- #
def resolve(
    interpretation: PeriodInterpretation,
    *,
    fiscal_year_end: str | None = None,
) -> ResolvedWindow:
    """Turn one interpretation into a concrete date window.

    Args:
        interpretation: from :func:`interpret`.
        fiscal_year_end: the issuer's year end as ``MMDD`` — the shape the SEC
            submissions JSON uses in its ``fiscalYearEnd`` field (Microsoft is
            ``"0630"``).  Required for a fiscal reading; ignored for a calendar
            one.

    A **fiscal year N** is the year *ending* on ``fiscal_year_end`` in calendar
    year N.  That is the SEC/issuer convention, and it is what makes Microsoft's
    FY2026 run July 2025 - June 2026, so that its Q2 ends in December 2025.

    Raises:
        ValueError: for a fiscal reading with no or malformed ``fiscal_year_end``.
    """
    kind, year, ordinal = interpretation.kind, interpretation.year, interpretation.ordinal

    if interpretation.basis == "calendar":
        year_start = date(year, 1, 1)
    else:
        month, day = _parse_fiscal_year_end(fiscal_year_end)
        # The fiscal year opens the day after the PREVIOUS year end, computed
        # independently rather than by stepping 12 months back from this one.
        # Stepping back re-uses this year's already-clamped day and makes
        # consecutive years overlap: with a 29 February year end, FY2024 ends
        # 2024-02-29 while a 12-month step back from FY2025's 2025-02-28 lands on
        # that same 2024-02-29, so one day belongs to both years.
        year_start = _safe_date(year - 1, month, day) + timedelta(days=1)

    months = _MONTHS_IN[kind]
    offset = 0 if ordinal is None else (ordinal - 1) * months
    start = _add_months(year_start, offset)
    # The end is the day before the NEXT sub-period starts, with both boundaries
    # anchored on the year's start rather than chained off each other. Chaining
    # carries a clamp forward: with a 30 January year end the year opens on the
    # 31st, Q2 would start on the 30th (clamped in April) and its end computed
    # from there would fall a day before Q3 opens, leaving one day in no quarter.
    end = _add_months(year_start, offset + months) - timedelta(days=1)
    return ResolvedWindow(interpretation=interpretation, start=start, end=end)


def _parse_fiscal_year_end(value: str | None) -> tuple[int, int]:
    """``"0630"`` -> ``(6, 30)``.

    Rejects rather than defaults: a fiscal reading computed against a guessed
    December year end would be a calendar reading wearing a fiscal label, which
    is the exact confusion this module exists to prevent.
    """
    if not value:
        raise ValueError(
            "a fiscal period needs the issuer's fiscal_year_end (MMDD, e.g. '0630'); "
            "the SEC submissions JSON carries it as 'fiscalYearEnd'"
        )
    digits = str(value).strip().replace("-", "").replace("/", "")
    if not re.fullmatch(r"\d{4}", digits):
        raise ValueError(f"fiscal_year_end must be MMDD digits, got {value!r}")
    month, day = int(digits[:2]), int(digits[2:])
    if not 1 <= month <= 12:
        raise ValueError(f"fiscal_year_end {value!r} is not a real month/day")
    # Checked against the month's own length, not a flat 1-31. "0231" is not a
    # typo we should quietly round to 28 February: it is corrupted issuer
    # metadata, and silently resolving it produces windows that look computed
    # rather than guessed. 29 February is allowed -- it is a real year end, and
    # `_safe_date` handles the non-leap years it has to live through.
    if not 1 <= day <= _DAYS_IN_MONTH[month]:
        raise ValueError(
            f"fiscal_year_end {value!r} is not a real month/day: "
            f"month {month:02d} has at most {_DAYS_IN_MONTH[month]} days"
        )
    return month, day


def _safe_date(year: int, month: int, day: int) -> date:
    """``date`` clamped to the month's real length.

    A filer whose year ends on the 31st has no 31 February to end on in a
    transition year; clamping to the 28th/29th is what the calendar does, and
    raising here would fail a lookup over a date nobody asked about directly.
    """
    for candidate in range(day, 0, -1):
        try:
            return date(year, month, candidate)
        except ValueError:
            continue
    raise ValueError(f"cannot build a date from {year}-{month}-{day}")


def _add_months(anchor: date, months: int) -> date:
    total = anchor.month - 1 + months
    return _safe_date(anchor.year + total // 12, total % 12 + 1, anchor.day)


def nominal_days(kind: PeriodKind) -> int:
    """Nominal length of ``kind`` in days — the SEC frames convention."""
    return _NOMINAL_DAYS[kind]


__all__ = [
    "Basis",
    "PeriodInterpretation",
    "PeriodKind",
    "PeriodParseError",
    "ResolvedWindow",
    "interpret",
    "nominal_days",
    "resolve",
]
