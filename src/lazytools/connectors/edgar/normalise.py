"""Building a normalised base from a filing's own statements.

The division of labour is the point. A model decides **which presented line is
which element**, because that is semantic and a table of XBRL concepts does it
badly — guessed filer by filer, and wrong for the next one. Code decides
**everything else**: which column covers the period, what scale applies, whether
an aggregate survives its own components, and what state a figure is allowed to
carry. The model never supplies a number; it names a line, and the value is read
out of the parsed statement.

So a fabricated figure is not something to detect here. It is something the
interface between the two cannot express.

The order is not negotiable. Classification runs before anything is fetched — a
run that trusted the SIC spent twenty requests on Deere's debt before finding it
had been reading a captive-finance group as an ordinary industrial. Checks run
before a figure is admitted, because the failure that matters does not error:
Cisco's entity-wide amortisation resolves cleanly, carries perfect provenance,
and is a third of the real number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from lazytools.connectors.edgar.client import EdgarService
from lazytools.connectors.edgar.mapping import DEFAULT_MODEL, Mapping, propose
from lazytools.connectors.edgar.ontology import classify
from lazytools.connectors.edgar.statements import (
    RenderedStatement,
    list_reports,
    read_statement,
)
from lazytools.financials.normalised import (
    ELEMENTS,
    Check,
    Contribution,
    Element,
    NormalisedBase,
)
from lazytools.financials.period import ResolvedWindow, interpret, resolve
from lazytools.financials.reconcile import reconcile

#: Reports whose lines are offered for mapping. The primary statements always;
#: the notes only when their name suggests they carry a figure the base wants,
#: because a 10-K renders well over a hundred reports and most are prose.
_NOTE_PATTERN = re.compile(
    r"lease|debt|borrowing|maturit|cash|restricted|intangible|amorti|depreciat|"
    r"credit facilit|revolv",
    re.I,
)
#: How much of a figure's own components may go missing before the figure is a
#: floor rather than the figure.
_TOTALS: dict[str, tuple[str, ...]] = {
    "reported_financial_debt": ("short_term_borrowings", "current_long_term_debt",
                                "long_term_debt_noncurrent"),
    "operating_da_total": ("depreciation", "amortisation_intangibles"),
}


@dataclass(frozen=True)
class _Presented:
    """One mapped line, resolved back to the value the statement actually shows."""

    element_id: str
    value: float
    statement: str
    label: str
    concept: str | None
    note: str


def normalise(
    client: EdgarService,
    *,
    company: str,
    period: str,
    as_of: date | None = None,
    currency: str = "USD",
    agent: Any | None = None,
    model: str = DEFAULT_MODEL,
) -> NormalisedBase:
    """Produce the normalised base for one issuer and one annual period.

    Args:
        client: any :class:`EdgarService`.
        company: ticker, name or CIK.
        period: an annual phrase such as ``"FY2024"``.
        as_of: ignore filings after this date, so the result is reproducible.
        currency: recorded on the base; the statements carry their own scale.
        agent: an injected mapping callable, mostly for tests.
        model: the model used for mapping when no agent is injected.

    Classification rides on the result rather than stopping it: a caller needs
    to know that Cisco has a finance business, not to be refused an answer.
    """
    matches = client.resolve_company(company, limit=1)
    if not matches:
        raise ValueError(f"{company!r} matched no EDGAR filer")
    cik = matches[0]["cik"]
    profile = client.issuer_profile(cik)

    filing = _annual_filing(client, cik, as_of)
    if filing is None:
        raise ValueError(f"no annual filing for {company!r} on or before "
                         f"{as_of or 'today'}")
    accession = filing["accession_no"]

    gate = classify(client, cik, accession)
    window = resolve(interpret(period)[0], fiscal_year_end=profile.get("fiscal_year_end"))
    statements = _read_statements(client, cik, accession)
    column = _column_for(statements, window)

    elements: dict[str, Element] = {}
    if column is None:
        mapping = Mapping(refs=(), absences=(), rejected=(
            f"no rendered column covers {window.start}..{window.end}",))
    else:
        mapping = propose(statements, column=column, agent=agent, model=model)
        presented = _resolve_refs(mapping, statements, column)
        for element_id, line in presented.items():
            value, note = _apply_sign(element_id, line.value)
            elements[element_id] = Element(
                element_id, value, "reported",
                route=f"{line.statement}: {line.label!r}" + (f" ({note})" if note else ""),
                sources=(f"{accession} / {line.statement}"
                         + (f" / us-gaap:{line.concept}" if line.concept else ""),))
        _check_totals(elements, presented)
        _derive(elements)

    for absence in mapping.absences:
        elements.setdefault(absence.element_id, Element(
            absence.element_id, None, "unavailable",
            blocked_reason=f"not present in the filing's statements: {absence.reason}"))

    return NormalisedBase(
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


# --------------------------------------------------------------------------- #
# Reading the filing
# --------------------------------------------------------------------------- #
def _annual_filing(client: EdgarService, cik: str, as_of: date | None) -> dict[str, Any] | None:
    for form in ("10-K", "20-F", "40-F"):
        for filing in client.list_filings(cik, form=form, limit=10):
            filed = filing.get("filed_at", "")
            if as_of is None or (filed and filed <= as_of.isoformat()):
                return filing
    return None


def _read_statements(client: EdgarService, cik: str, accession: str) -> list[RenderedStatement]:
    """The primary statements, plus the notes that plausibly carry a base figure."""
    try:
        reports = list_reports(client, cik, accession)
    except ValueError:
        return []
    wanted = [r for r in reports
              if r.is_primary_statement or _NOTE_PATTERN.search(r.short_name)]
    statements: list[RenderedStatement] = []
    for report in wanted:
        try:
            statements.append(read_statement(client, cik, accession, report))
        except Exception:  # noqa: BLE001 - one unreadable report is not a failed filing
            continue
    return statements


def _column_for(statements: list[RenderedStatement], window: ResolvedWindow) -> int | None:
    """The column whose end date covers the window, agreed across statements."""
    for statement in statements:
        for index, header in enumerate(statement.columns):
            parsed = _header_date(header)
            if parsed and abs((parsed - window.end).days) <= window.tolerance_days:
                return index
    return None


def _header_date(header: str) -> date | None:
    for fmt in ("%b. %d, %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(header.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _resolve_refs(
    mapping: Mapping, statements: list[RenderedStatement], column: int
) -> dict[str, _Presented]:
    """Read each referenced line's value out of the parsed statement.

    A reference that matches no line is dropped: the model named something that
    is not there, and inventing a value for it is the one thing this design
    exists to prevent. A reference matching several lines is dropped too — the
    ambiguity is real, and picking the first would resolve it by luck.
    """
    resolved: dict[str, _Presented] = {}
    for ref in mapping.refs:
        hits = [
            (s, line) for s in statements
            if not ref.statement or ref.statement.lower() in s.title.lower()
            for line in s.lines
            if line.label.strip().lower() == ref.label.strip().lower()
            and column < len(line.values) and line.values[column] is not None
        ]
        if len(hits) != 1 or ref.element_id in resolved:
            continue
        statement, line = hits[0]
        resolved[ref.element_id] = _Presented(
            element_id=ref.element_id, value=line.values[column],  # type: ignore[arg-type]
            statement=statement.report.short_name, label=line.label,
            concept=line.tag, note=ref.note)
    return resolved


# --------------------------------------------------------------------------- #
# Checks and derivations, which the model has no part in
# --------------------------------------------------------------------------- #
def _apply_sign(element_id: str, value: float) -> tuple[float, str]:
    """Put a magnitude element on the sign its definition expects.

    A rendered cash-flow statement shows capex, dividends and buybacks as
    negative because they are outflows in that presentation; the same figures
    arrive positive as XBRL facts. Neither is wrong, and an element that sums
    them cannot state its own signs unless the inputs agree on one. Elements
    whose sign IS information -- operating income, free cash flow, a
    working-capital movement -- are left exactly as presented.
    """
    if ELEMENTS[element_id].sign != "magnitude" or value >= 0:
        return value, ""
    return -value, "presented as an outflow; stored as a magnitude"


def _check_totals(elements: dict[str, Element], presented: dict[str, _Presented]) -> None:
    """Reconcile every mapped total against its mapped components."""
    for total_id, component_ids in _TOTALS.items():
        if total_id not in elements:
            continue
        components = {cid: presented[cid].value if cid in presented else None
                      for cid in component_ids}
        if not any(v is not None for v in components.values()):
            continue
        result = reconcile(total_id, elements[total_id].value, components)
        current = elements[total_id]
        if result.status == "balanced":
            elements[total_id] = Element(
                total_id, current.value, "verified", route=current.route,
                sources=current.sources,
                checks=(Check("components", True, result.detail),))
        elif result.blocking and result.status != "incomplete":
            elements[total_id] = Element(
                total_id, current.value, "unreconciled", route=current.route,
                checks=(Check("components", False, result.detail),),
                blocked_reason=result.detail)


def _derive(elements: dict[str, Element]) -> None:
    """Everything the base computes rather than reads."""
    _sum_into(elements, "reported_financial_debt", _TOTALS["reported_financial_debt"])
    _sum_into(elements, "operating_da_total", _TOTALS["operating_da_total"])
    _combine(elements, "house_operating_ebitda",
             {"operating_income": 1, "operating_da_total": 1})
    _combine(elements, "house_ffo",
             {"house_operating_ebitda": 1, "cash_interest_paid": -1, "cash_taxes_paid": -1})
    _combine(elements, "house_capex", {"capex_ppe": 1, "capex_intangibles": 1}, optional=("capex_intangibles",))
    _combine(elements, "focf", {"cfo": 1, "house_capex": -1})
    _combine(elements, "dcf", {"focf": 1, "dividends_paid": -1, "share_repurchases": -1},
             optional=("share_repurchases",))
    _combine(elements, "house_adjusted_debt",
             {"reported_financial_debt": 1, "finance_lease_total": 1, "operating_lease_total": 1},
             note="house convention: leases capitalised")
    _combine(elements, "readily_available_cash", {"cash_and_equivalents": 1})
    _combine(elements, "house_net_debt",
             {"house_adjusted_debt": 1, "readily_available_cash": -1})


def _sum_into(elements: dict[str, Element], total_id: str, component_ids: tuple[str, ...]) -> None:
    """Build a total from its components when the filing presented no total."""
    if total_id in elements:
        return
    known = {cid: elements[cid].value for cid in component_ids
             if cid in elements and elements[cid].usable}
    if not known:
        elements[total_id] = Element(total_id, None, "unavailable",
                                     blocked_reason="neither the total nor any component was found")
    elif len(known) < len(component_ids):
        elements[total_id] = Element(
            total_id, sum(known.values()), "lower_bound", route=" + ".join(known),
            blocked_reason=f"{len(component_ids) - len(known)} component(s) not found, "
                           "so this is a floor and not the figure")
    else:
        elements[total_id] = Element(
            total_id, sum(known.values()), "derived", route=" + ".join(known),
            contributions=tuple(Contribution(k, v) for k, v in known.items()))


def _combine(
    elements: dict[str, Element],
    target: str,
    terms: dict[str, int],
    *,
    optional: tuple[str, ...] = (),
    note: str = "",
) -> None:
    """A signed sum of other elements, blocked when a required term is not usable."""
    if target in elements and elements[target].usable:
        return
    values: dict[str, float] = {}
    for key, sign in terms.items():
        element = elements.get(key)
        if element is not None and element.usable:
            values[key] = sign * (element.value or 0.0)
        elif key not in optional:
            elements[target] = Element(
                target, None, "unavailable",
                blocked_reason=f"{key} is not usable, so everything built on it falls with it")
            return
    elements[target] = Element(
        target, sum(values.values()), "derived",
        route=note or " ".join(f"{'+' if v >= 0 else '-'} {k}" for k, v in values.items()).lstrip("+ "),
        contributions=tuple(Contribution(k, v) for k, v in values.items()))


__all__ = ["normalise"]
