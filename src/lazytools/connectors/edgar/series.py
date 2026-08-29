"""Several periods of one issuer, and what makes them comparable.

One period answers almost nothing. Whether a working-capital release is prudent
management or a business selling down its ability to trade, whether a margin is
at a cyclical peak, whether leverage is rising — none of it is visible in a
single column. The credit analyst says so itself, unprompted, in every note
built from one year: direction is a fact, a level is a judgement.

**A filing already carries several periods.** An income statement typically
presents three years and a balance sheet two, and those columns are the best
multi-period source there is — not because they are convenient but because the
filer presented them together, on one basis, restating earlier years where its
own accounting changed. They are comparable because the issuer made them so.

That is also why one mapping serves all of them: a mapping names lines, and the
label "Total revenue" is the same line whichever column you read. Three periods
from one filing cost one model call, not three — and three calls could return
three different answers about a document that cannot change.

**Across filings, the same year can carry two different values.** A year in its
own annual report and the same year as the comparative in the next one may
differ: restatement, reclassification, an operation moved to discontinued. Both
are correct; they answer "what did the issuer say then" and "what does the
issuer say now", which are different questions. This module reports the
disagreement rather than choosing for you, because a restatement is a finding
about the issuer and not noise to be smoothed away.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from lazytools.connectors.edgar.client import EdgarService
from lazytools.connectors.edgar.mapping import DEFAULT_MODEL
from lazytools.connectors.edgar.mapping_store import MappingStore
from lazytools.connectors.edgar.normalise import (
    ANNUAL_FORMS,
    _columns_for,
    _elements_for,
    _header_date,
    _mapping_for,
    _read_statements,
)
from lazytools.connectors.edgar.ontology import classify
from lazytools.financials.normalised import NormalisedBase
from lazytools.financials.period import ResolvedWindow, interpret, resolve

#: How far apart two readings of one figure may sit before it is a restatement
#: rather than rounding. Presented statements are rounded to the table's unit,
#: so an exact comparison reports noise; a tenth of a percent is far below any
#: restatement worth naming and far above any rounding difference.
RESTATEMENT_TOLERANCE = 0.001


@dataclass(frozen=True)
class Restatement:
    """One figure that two filings report differently for the same period."""

    element_id: str
    period_end: date
    #: As the period's own annual report gave it.
    first_reported: float
    first_accession: str
    #: As a later filing gave it, presenting the same period as a comparative.
    later_reported: float
    later_accession: str

    @property
    def change(self) -> float:
        return self.later_reported - self.first_reported

    def __str__(self) -> str:
        return (f"{self.element_id} for the period ended {self.period_end}: "
                f"{self.first_reported:,.0f} in {self.first_accession}, "
                f"{self.later_reported:,.0f} in {self.later_accession}")


@dataclass(frozen=True)
class Series:
    """Several periods of one issuer, oldest first.

    ``periods`` are complete normalised bases, so anything that reads one base
    reads these. What the series adds is the two things a single base cannot
    carry: the order, and the disagreements between filings about the same year.
    """

    issuer_name: str
    cik: str
    periods: tuple[NormalisedBase, ...]
    restatements: tuple[Restatement, ...]
    #: Accessions read, newest first. A series drawn from one filing is on one
    #: basis; a series drawn from several is not, and this says which it is.
    accessions: tuple[str, ...]

    @property
    def single_basis(self) -> bool:
        """Whether every period came from one filing, and so from one basis."""
        return len(self.accessions) == 1

    def value(self, element_id: str) -> list[tuple[date, float | None]]:
        """One element across the series, oldest first.

        ``None`` where the period could not establish it — which is a fact about
        that period and not a gap to interpolate.
        """
        return [
            (base.period_end,
             element.value if (element := base.elements.get(element_id)) and element.usable
             else None)
            for base in self.periods
        ]


def normalise_series(
    client: EdgarService,
    *,
    company: str,
    years: int = 3,
    as_of: date | None = None,
    currency: str = "USD",
    agent: Any | None = None,
    model: str = DEFAULT_MODEL,
    store: MappingStore | None = None,
) -> Series:
    """Normalised bases for the most recent ``years`` annual periods.

    Args:
        client: any :class:`EdgarService`.
        company: ticker, name or CIK.
        years: how many annual periods to return. Fewer come back when the
            issuer has not filed that many; that is an answer, not an error.
        as_of: ignore filings after this date, so a run is reproducible.
        currency: recorded on each base.
        agent: an injected mapping callable, mostly for tests.
        model: the model used for mapping when no agent is injected.
        store: the mapping cache. Worth passing: a second run over the same
            filings costs nothing and returns the same series.

    Walks back through annual filings only as far as it must. The newest filing
    usually supplies three years by itself, so a three-year series is one filing
    and one model call; a longer one reaches into earlier filings, and every
    period they overlap on is compared rather than assumed to agree.
    """
    matches = client.resolve_company(company, limit=1)
    if not matches:
        raise ValueError(f"{company!r} matched no EDGAR filer")
    cik = matches[0]["cik"]
    profile = client.issuer_profile(cik)
    year_end = profile.get("fiscal_year_end")

    collected: dict[date, NormalisedBase] = {}
    restatements: list[Restatement] = []
    accessions: list[str] = []

    for filing in _annual_filings(client, cik, as_of, wanted=years):
        if len(collected) >= years:
            break
        accession = filing["accession_no"]
        statements = _read_statements(client, cik, accession)
        windows = _windows_in(statements, year_end)
        if not windows:
            continue
        # An unreadable filing is skipped, not fatal. Breaking the walk here
        # ended a six-year series at whatever the last good filing covered, and
        # said nothing about having done so.
        primary = _columns_for(statements, windows[0])
        if not primary:
            continue

        accessions.append(accession)
        gate = classify(client, cik, accession)
        mapping = _mapping_for(primary, accession, agent, model, store)

        for window in windows:
            columns = _columns_for(statements, window)
            if not columns:
                continue
            base = NormalisedBase(
                issuer_name=str(profile.get("name") or company),
                cik=cik,
                ontology=gate.ontology,
                open_signals=gate.signal_names,
                accounting_standard="us-gaap",
                currency=currency,
                period_start=window.start,
                period_end=window.end,
                information_cutoff=as_of or datetime.now(timezone.utc).date(),
                perimeter_status="unavailable",
                accession=accession,
                elements=_elements_for(mapping, columns, accession),
            )
            existing = collected.get(window.end)
            if existing is None:
                if len(collected) < years:
                    collected[window.end] = base
            else:
                # Filings are walked newest first, so anything already held came
                # from a LATER filing than this one.
                restatements.extend(_disagreements(earlier=base, later=existing))

    ordered = tuple(collected[key] for key in sorted(collected))
    return Series(
        issuer_name=str(profile.get("name") or company),
        cik=cik,
        periods=ordered,
        restatements=tuple(restatements),
        accessions=tuple(accessions),
    )


def _annual_filings(
    client: EdgarService, cik: str, as_of: date | None, *, wanted: int
) -> list[dict[str, Any]]:
    """Every annual filing on or before ``as_of``, newest first.

    Fetched once. Re-querying with a moving cutoff meant a request per step —
    and each step past the first had to enable EDGAR's paginated history, so a
    six-year series walked the history three times to find three filings.

    Sorted across forms rather than within them. Taking all 10-Ks before any
    20-F puts a filer that changed form out of date order, and the newest-first
    invariant is what makes an already-held period the LATER presentation.
    """
    filings: dict[str, dict[str, Any]] = {}
    for form in ANNUAL_FORMS:
        for filing in client.list_filings(cik, form=form, limit=max(4, wanted + 2),
                                          include_history=True):
            filed = filing.get("filed_at", "")
            if as_of is None or (filed and filed <= as_of.isoformat()):
                filings[filing["accession_no"]] = filing
    return sorted(filings.values(), key=lambda f: f.get("filed_at", ""), reverse=True)


def _windows_in(statements: list[Any], year_end: str | None) -> list[ResolvedWindow]:
    """The annual windows a filing actually presents, newest first.

    Read from the statements' own column headers rather than assumed. The header
    gives a period END; the fiscal year label that produces it is whichever of
    the two candidate years resolves to a window ending on that date, which is
    the only reliable way to name a year for a filer whose year ends in January.
    """
    ends: set[date] = set()
    for statement in statements:
        for header in statement.columns:
            parsed = _header_date(header)
            if parsed is not None:
                ends.add(parsed)

    windows: dict[date, ResolvedWindow] = {}
    for end in ends:
        for label in (f"FY{end.year}", f"FY{end.year + 1}"):
            for interpretation in interpret(label):
                if interpretation.kind != "annual":
                    continue
                window = resolve(interpretation, fiscal_year_end=year_end)
                if abs((window.end - end).days) <= window.tolerance_days:
                    windows.setdefault(end, window)
                    break
            if end in windows:
                break
    return [windows[key] for key in sorted(windows, reverse=True)]


def _disagreements(*, earlier: NormalisedBase, later: NormalisedBase) -> list[Restatement]:
    """Elements the two filings give different values for, same period.

    Only figures both filings actually established are compared: an element one
    of them could not place says nothing about the other, and reporting that as
    a restatement would bury the real ones.
    """
    found: list[Restatement] = []
    for element_id, first in earlier.elements.items():
        second = later.elements.get(element_id)
        if second is None:
            continue
        # Only figures both filings actually READ from a statement. A derived
        # element inherits its inputs, so one restated line would be reported
        # again as a restatement of EBITDA, of FFO, of free cash flow and of the
        # residual — four findings where the issuer made one change, and the
        # real one buried among its own consequences.
        if {first.state, second.state} - {"reported", "verified"}:
            continue
        if first.value is None or second.value is None:
            continue
        scale = max(abs(first.value), abs(second.value), 1.0)
        if abs(second.value - first.value) / scale <= RESTATEMENT_TOLERANCE:
            continue
        found.append(Restatement(
            element_id=element_id, period_end=earlier.period_end,
            first_reported=first.value, first_accession=earlier.accession,
            later_reported=second.value, later_accession=later.accession))
    return found


__all__ = ["RESTATEMENT_TOLERANCE", "Restatement", "Series", "normalise_series"]
