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
from datetime import date, datetime, timedelta, timezone
from typing import Any

from lazytools.connectors.edgar.client import EdgarService
from lazytools.connectors.edgar.mapping import DEFAULT_MODEL
from lazytools.connectors.edgar.mapping_store import MappingStore
from lazytools.connectors.edgar.normalise import (
    _annual_filing,
    _columns_for,
    _elements_for,
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
    cutoff = as_of

    while len(collected) < years:
        # The first filing is always in EDGAR's recent block; anything earlier
        # may not be, and looking only there makes an issuer appear to have
        # stopped filing.
        filing = _annual_filing(client, cik, cutoff, deep=bool(accessions))
        if filing is None:
            break
        accession = filing["accession_no"]
        if accession in accessions:
            break
        accessions.append(accession)

        statements = _read_statements(client, cik, accession)
        windows = _windows_in(statements, year_end)
        if not windows:
            break

        gate = classify(client, cik, accession)
        # One mapping for the whole filing, taken against its most recent
        # column: the refs name lines, and a line is the same line in every
        # column. Mapping per period would pay per year for one document.
        primary = _columns_for(statements, windows[0])
        if not primary:
            break
        mapping = _mapping_for(primary, accession, agent, model, store)

        for window in windows:
            columns = _columns_for(statements, window)
            if not columns:
                continue
            elements = _elements_for(mapping, columns, accession)
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
                elements=elements,
            )
            existing = collected.get(window.end)
            if existing is None:
                if len(collected) < years:
                    collected[window.end] = base
            else:
                # The period is already held from a NEWER filing, so `existing`
                # is the later presentation and `base` the original one.
                restatements.extend(_disagreements(earlier=base, later=existing))

        # Step back to the filing that reported our oldest year as its OWN most
        # recent year, rather than to the one before this filing's whole span.
        #
        # Jumping the whole span is cheaper — two filings cover six years — but
        # it lands on a filing that shares no period with what we hold, so the
        # two are never compared and a restatement can never be found. Landing
        # one year back overlaps by a year, which is what makes the comparison
        # real; it costs a filing per two years instead of per three.
        #
        # An annual report is filed within about three months of its year end,
        # and the next one about fifteen months after that, so a cutoff 300 days
        # past the year end selects that year's filing and not the following
        # one.
        cutoff = min(windows, key=lambda w: w.end).end + timedelta(days=300)

    ordered = tuple(collected[key] for key in sorted(collected))
    return Series(
        issuer_name=str(profile.get("name") or company),
        cik=cik,
        periods=ordered,
        restatements=tuple(restatements),
        accessions=tuple(accessions),
    )


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


def _header_date(header: str) -> date | None:
    for fmt in ("%b. %d, %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(header.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _disagreements(*, earlier: NormalisedBase, later: NormalisedBase) -> list[Restatement]:
    """Elements the two filings give different values for, same period.

    Only figures both filings actually established are compared: an element one
    of them could not place says nothing about the other, and reporting that as
    a restatement would bury the real ones.
    """
    found: list[Restatement] = []
    for element_id, first in earlier.elements.items():
        second = later.elements.get(element_id)
        if not (first.usable and second is not None and second.usable):
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
