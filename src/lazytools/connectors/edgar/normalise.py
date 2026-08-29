"""Building a normalised base from a filing's own statements.

The division of labour is the point. A model decides **which presented line is
which element**, because that is semantic and a table of XBRL concepts does it
badly — guessed filer by filer, and wrong for the next one. Code decides
**everything else**: which column of which statement covers the period, what
scale applies, whether an aggregate survives its own components, and what state
a figure is allowed to carry. The model never supplies a number; it names a
line, and the value is read out of the parsed statement.

So a fabricated figure is not something to detect here. It is something the
interface between the two cannot express.

The order is not negotiable. Classification runs before anything is fetched — a
run that trusted the SIC spent twenty requests on Deere's debt before finding it
had been reading a captive-finance group as an ordinary industrial. Checks run
before a figure is admitted, because the failure that matters does not error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from lazytools.connectors.edgar.client import EdgarService
from lazytools.connectors.edgar.mapping import DEFAULT_MODEL, Mapping, propose
from lazytools.connectors.edgar.mapping_store import MappingStore
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

#: Reports offered for mapping: the primary statements always, and the notes
#: whose name suggests they carry a figure the base wants. A 10-K renders well
#: over a hundred reports and most of them are prose.
_NOTE_PATTERN = re.compile(
    r"lease|debt|borrowing|maturit|cash|restricted|intangible|amorti|depreciat|"
    r"credit facilit|revolv",
    re.I,
)
#: Totals whose components the base also carries. A total is only as good as the
#: components that confirm it, and one presented without them is a claim.
_TOTALS: dict[str, tuple[str, ...]] = {
    "reported_financial_debt": ("short_term_borrowings", "current_long_term_debt",
                                "long_term_debt_noncurrent"),
    "operating_da_total": ("depreciation", "amortisation_intangibles"),
    "operating_lease_total": ("operating_lease_current", "operating_lease_noncurrent"),
    "finance_lease_total": ("finance_lease_current", "finance_lease_noncurrent"),
}

#: Wholes the model never sees, because they are computed here, but which still
#: settle a line their parts both claim. Every part must enter its whole with
#: weight +1, which is what makes awarding a combined line to any one of them
#: harmless: the whole comes out identical either way.
_EQUAL_WEIGHT_WHOLES: dict[str, tuple[str, ...]] = {
    "house_capex": ("capex_ppe", "capex_intangibles", "lease_fleet_investment"),
}

#: Every whole/part relation the resolver knows, presented or computed.
_WHOLES: dict[str, tuple[str, ...]] = {**_TOTALS, **_EQUAL_WEIGHT_WHOLES}

#: Elements that measure DEBT and must never be read off a lease table.
#:
#: NVIDIA files no debt maturity schedule at all, so the model took the lease
#: commitment schedule instead: the years line up, the labels look right, and
#: the result was a $2.1bn maturity ladder for an issuer with $8.5bn of debt.
#: Nothing downstream could have caught it — the figures were real, correctly
#: scaled, and from the filing. Only their scope was wrong.
_DEBT_SCOPED = frozenset({
    "debt_maturity_y1", "debt_maturity_y2", "debt_maturity_y3", "debt_maturity_y4",
    "debt_maturity_y5", "debt_maturity_thereafter", "reported_financial_debt",
    "short_term_borrowings", "current_long_term_debt", "long_term_debt_noncurrent",
})
_LEASE_TABLE = re.compile(r"\blease", re.I)


@dataclass(frozen=True)
class _Column:
    """One statement and the column index within it that covers the period.

    Resolved per statement rather than once. A balance sheet shows two dates and
    an income statement three, and nothing makes the period sit at the same
    index in both — one global index silently reads revenue from one year and
    cash flow from another.
    """

    statement: RenderedStatement
    index: int


@dataclass(frozen=True)
class _Presented:
    """One mapped line, resolved back to the value the statement actually shows."""

    element_id: str
    value: float
    statement: str
    label: str
    concept: str | None


def normalise(
    client: EdgarService,
    *,
    company: str,
    period: str,
    as_of: date | None = None,
    currency: str = "USD",
    agent: Any | None = None,
    model: str = DEFAULT_MODEL,
    store: MappingStore | None = None,
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
        store: a mapping cache. A filed document never changes, so its mapping
            is a property of the document: caching it makes a second run over
            the same filing both free and identical to the first.

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
        raise ValueError(f"no annual filing for {company!r} on or before {as_of or 'today'}")
    accession = filing["accession_no"]

    gate = classify(client, cik, accession)
    window = resolve(interpret(period)[0], fiscal_year_end=profile.get("fiscal_year_end"))
    columns = _columns_for(_read_statements(client, cik, accession), window)

    if not columns:
        mapping = Mapping(refs=(), absences=(), rejected=(
            f"no rendered column covers {window.start}..{window.end}",))
    else:
        mapping = _mapping_for(columns, accession, agent, model, store)
    elements = _elements_for(mapping, columns, accession)

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


def _elements_for(
    mapping: Mapping, columns: list[_Column], accession: str
) -> dict[str, Element]:
    """Every element for ONE period, from a mapping already made.

    Separated from :func:`normalise` because a mapping is a property of the
    document and not of the period: the label "Total revenue" names the same
    line whichever column you read. So one mapping serves every period a filing
    presents, and a series can reuse it instead of asking the model once per
    year — which would also risk three different answers about one document.
    """
    elements: dict[str, Element] = {}
    for element_id, line in _resolve_refs(mapping, columns).items():
        value, note = _apply_sign(element_id, line.value)
        elements[element_id] = Element(
            element_id, value, "reported",
            route=f"{line.statement}: {line.label!r}" + (f" ({note})" if note else ""),
            sources=(f"{accession} / {line.statement}"
                     + (f" / us-gaap:{line.concept}" if line.concept else ""),))
    if columns:
        _check_totals(elements, columns)
        _derive(elements)
    for absence in mapping.absences:
        elements.setdefault(absence.element_id, Element(
            absence.element_id, None, "unavailable",
            blocked_reason=f"not present in the filing's statements: {absence.reason}"))
    return elements


# --------------------------------------------------------------------------- #
# Reading the filing
# --------------------------------------------------------------------------- #
def _annual_filing(
    client: EdgarService, cik: str, as_of: date | None, *, deep: bool = False
) -> dict[str, Any] | None:
    """The most recent annual filing on or before ``as_of``.

    ``deep`` reaches past EDGAR's recent-filings block into the paginated
    history. Off by default because it costs extra requests and the newest
    filing is always in the recent block; on when walking backwards, where
    leaving it off makes an issuer look as though it stopped filing. Walmart
    returns three 10-Ks without it and its whole record with it, which is how a
    six-year series quietly came back with three years.
    """
    for form in ("10-K", "20-F", "40-F"):
        for filing in client.list_filings(cik, form=form, limit=10 if not deep else 40,
                                          include_history=deep):
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
    statements: list[RenderedStatement] = []
    for report in reports:
        if not (report.is_primary_statement or _NOTE_PATTERN.search(report.short_name)):
            continue
        try:
            statements.append(read_statement(client, cik, accession, report))
        except Exception:  # noqa: BLE001 - one unreadable report is not a failed filing
            continue
    return statements


def _columns_for(statements: list[RenderedStatement], window: ResolvedWindow) -> list[_Column]:
    """Each statement paired with ITS column covering the window.

    A statement with no such column is dropped rather than read at some other
    index: its figures are for another period, and mixing them with the right
    ones is the error this replaced.
    """
    columns: list[_Column] = []
    for statement in statements:
        for index, header in enumerate(statement.columns):
            parsed = _header_date(header)
            if parsed and abs((parsed - window.end).days) <= window.tolerance_days:
                columns.append(_Column(statement, index))
                break
    return columns


def _header_date(header: str) -> date | None:
    for fmt in ("%b. %d, %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(header.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _mapping_for(
    columns: list[_Column], accession: str, agent: Any | None, model: str,
    store: MappingStore | None,
) -> Mapping:
    """The mapping, from the cache when it is there and from a model when not."""
    # The cache key needs one column index; the statements are all at the same
    # period, so the first is representative of the request.
    key_index = columns[0].index
    if store is not None:
        cached = store.get(accession, key_index)
        if cached is not None:
            return cached.mapping
    mapping = propose([c.statement for c in columns], column=key_index,
                      agent=agent, model=model)
    if store is not None and mapping.refs:
        # Only a mapping that placed something is worth keeping: caching a failed
        # model run would make its failure permanent.
        store.put(accession, key_index, mapping, model=model)
    return mapping


def _resolve_refs(mapping: Mapping, columns: list[_Column]) -> dict[str, _Presented]:
    """Read each referenced line's value out of the parsed statement.

    Three things are dropped rather than resolved, and each was a real error:

    * a reference matching no line — the model named something that is not
      there, and inventing a value for it is what this design prevents;
    * a reference matching several lines — the ambiguity is real, and taking the
      first resolves it by luck;
    * a LINE claimed by more than one element, where the claims genuinely
      conflict — settling that by which claim the model happened to emit first
      is the same coin flip. A total claimed alongside its own declared
      components is NOT such a conflict; see :func:`_settle`.

    And one is dropped for its SCOPE rather than its resolution: a debt figure
    read off a lease table. The value is real and the label is plausible, which
    is exactly why nothing downstream would question it.
    """
    hits: dict[str, tuple[_Column, Any]] = {}
    claims: dict[tuple[str, str], list[str]] = {}
    for ref in mapping.refs:
        if ref.element_id in hits:
            continue
        found = [
            (column, line) for column in columns
            if not ref.statement or ref.statement.lower() in column.statement.title.lower()
            for line in column.statement.lines
            if line.label.strip().lower() == ref.label.strip().lower()
            and column.index < len(line.values) and line.values[column.index] is not None
        ]
        if len(found) != 1:
            continue
        column, line = found[0]
        if ref.element_id in _DEBT_SCOPED and _LEASE_TABLE.search(column.statement.title):
            continue
        hits[ref.element_id] = (column, line)
        claims.setdefault((column.statement.report.short_name, line.label), []).append(ref.element_id)

    contested = {element_id for claimants in claims.values()
                 for element_id in _settle(claimants)}
    return {
        element_id: _Presented(
            element_id=element_id, value=line.values[column.index],
            statement=column.statement.report.short_name, label=line.label,
            concept=line.tag)
        for element_id, (column, line) in hits.items()
        if element_id not in contested
    }


def _settle(claimants: list[str]) -> set[str]:
    """Which claimants on one contested line to drop.

    A line claimed by two unrelated elements is genuinely ambiguous, and every
    claim on it goes: resolving a real conflict by which claim the model emitted
    first decides it with the order of a list.

    But a total and its own declared components are not in conflict. A filer
    showing one combined "Depreciation and amortization" line has presented the
    TOTAL, and the parts are not separable from it — so the total stands and the
    parts go. That is settled from the registry, which already declares the
    whole/part relation, rather than from the model. Dropping both instead
    cost Walmart its entire D&A, and EBITDA and FFO fell with it.

    Some wholes are computed and so are never offered to the model, which
    leaves a combined line claimed only by parts and no whole to award it to.
    NVIDIA presents one "Purchases related to property and equipment and
    intangible assets" line; it was claimed as both kinds of capital spend and
    both were dropped, taking free cash flow and the residual with them. Those
    parts sum into their whole with equal weight, so awarding the line to any
    one of them leaves the whole identical — the arithmetic cannot be changed
    by the choice, and the route carries the combined label, so a reader sees
    what the figure covers. The first part in declared order takes it.
    """
    if len(claimants) < 2:
        return set()
    wholes = [c for c in claimants if c in _WHOLES]
    if len(wholes) == 1:
        parts = [c for c in claimants if c != wholes[0]]
        if all(part in _WHOLES[wholes[0]] for part in parts):
            return set(parts)
    if not wholes:
        for whole, parts in _EQUAL_WEIGHT_WHOLES.items():
            if all(claimant in parts for claimant in claimants):
                keeps = next(part for part in parts if part in claimants)
                return {c for c in claimants if c != keeps}
    return set(claimants)

# --------------------------------------------------------------------------- #
# Checks and derivations, which the model has no part in
# --------------------------------------------------------------------------- #
def _apply_sign(element_id: str, value: float) -> tuple[float, str]:
    """Put a magnitude element on the sign its definition expects.

    A rendered cash-flow statement shows capex, dividends and buybacks negative
    because they are outflows in that presentation; the same figures arrive
    positive as XBRL facts. Neither is wrong, and an element that sums them
    cannot state its own signs unless the inputs agree on one. Elements whose
    sign IS information are left exactly as presented.
    """
    if ELEMENTS[element_id].sign != "magnitude" or value >= 0:
        return value, ""
    return -value, "presented as an outflow; stored as a magnitude"


def _rounding_unit(columns: list[_Column]) -> float:
    """The coarsest scale any statement was rendered at.

    Reconciling with no tolerance calls a table rendered in millions
    unreconciled whenever its own rounding does not sum exactly, which is often.
    """
    scales = [c.statement.money_scale for c in columns if c.statement.money_scale]
    return float(max(scales)) if scales else 0.0


def _check_totals(elements: dict[str, Element], columns: list[_Column]) -> None:
    """Reconcile every presented total against its components.

    A presented total whose components are missing is NOT left alone. It is
    exactly the shape that understated adjusted debt by 45%: a model mapped the
    current portion of a lease liability as the whole of it, the total looked
    reported and complete, and nothing downstream could tell.
    """
    unit = _rounding_unit(columns)
    for total_id, component_ids in _TOTALS.items():
        current = elements.get(total_id)
        if current is None or not current.usable:
            continue
        known = {cid: elements[cid].value for cid in component_ids
                 if cid in elements and elements[cid].usable}
        if not known:
            # A filer that presents one "Depreciation and amortization" line has
            # presented the total, and nothing here contradicts it. It stays
            # `reported` -- the weakest usable state, which is what one source
            # with nothing to check it against deserves.
            continue

        if len(known) == len(component_ids):
            result = reconcile(total_id, current.value, dict(known), rounding_unit=unit)
            if result.status == "balanced":
                elements[total_id] = Element(
                    total_id, current.value, "verified", route=current.route,
                    sources=current.sources, checks=(Check("components", True, result.detail),))
            elif result.blocking:
                elements[total_id] = Element(
                    total_id, current.value, "unreconciled", route=current.route,
                    sources=current.sources,
                    checks=(Check("components", False, result.detail),),
                    blocked_reason=result.detail)
            continue

        # Some components but not all. That cannot confirm a total, but it can
        # REFUTE one: parts already exceeding the whole is a contradiction
        # whatever the missing ones are. This is the shape that let the current
        # portion of a lease liability pass as the entire liability.
        subtotal = sum(known.values())
        tolerance = unit * (len(known) + 1) / 2
        if subtotal > (current.value or 0.0) + tolerance:
            detail = (f"the components read so far ({', '.join(known)}) already sum to "
                      f"{subtotal:,.0f} against a presented total of {current.value:,.0f}, "
                      "so the total is a part of itself")
            elements[total_id] = Element(
                total_id, current.value, "unreconciled", route=current.route,
                sources=current.sources,
                checks=(Check("components", False, detail),), blocked_reason=detail)


def _derive(elements: dict[str, Element]) -> None:
    """Everything the base computes rather than reads."""
    for total_id, component_ids in _TOTALS.items():
        _sum_into(elements, total_id, component_ids)
    _combine(elements, "house_operating_ebitda",
             {"operating_income": 1, "operating_da_total": 1})
    _combine(elements, "house_ffo",
             {"house_operating_ebitda": 1, "cash_interest_paid": -1, "cash_taxes_paid": -1})
    # Fleet investment is summed in, not excluded: it is cash the group really
    # spent, and CFO above it carries the lessor's operating flows too, so
    # netting one side without the other would overstate free cash flow for
    # every lessor. What it must not do is hide. Deere's 2,868m of equipment
    # bought to lease out was reaching the base as "capex_intangibles", which
    # named a fleet of machines after software; the analyst caught it from the
    # route label. Now it arrives under its own name and is visible as a
    # component, which is what lets a reader see that this capex is not the
    # industrial business's own.
    _combine(elements, "house_capex",
             {"capex_ppe": 1, "capex_intangibles": 1, "lease_fleet_investment": 1},
             optional=("capex_intangibles", "lease_fleet_investment"))
    _combine(elements, "focf", {"cfo": 1, "house_capex": -1})
    _combine(elements, "dcf", {"focf": 1, "dividends_paid": -1, "share_repurchases": -1},
             optional=("share_repurchases",))
    _combine(elements, "house_adjusted_debt",
             {"reported_financial_debt": 1, "finance_lease_total": 1, "operating_lease_total": 1},
             floor_when_missing=("finance_lease_total", "operating_lease_total"),
             note="reported debt plus both lease liabilities (house convention: leases "
                  "capitalised). Finance leases are added as presented; where an issuer "
                  "already includes them in reported debt this double counts, and the "
                  "filing's debt note is the only thing that settles it")
    _combine(elements, "readily_available_cash", {"cash_and_equivalents": 1})
    _combine(elements, "house_net_debt",
             {"house_adjusted_debt": 1, "readily_available_cash": -1})


def _sum_into(elements: dict[str, Element], total_id: str, component_ids: tuple[str, ...]) -> None:
    """Build a total from its components when the filing presented no usable one."""
    existing = elements.get(total_id)
    if existing is not None and existing.usable:
        return
    known = {cid: elements[cid].value for cid in component_ids
             if cid in elements and elements[cid].usable}
    if not known:
        if existing is None:
            elements[total_id] = Element(
                total_id, None, "unavailable",
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
    floor_when_missing: tuple[str, ...] = (),
    note: str = "",
) -> None:
    """A signed sum of other elements, blocked when a required term is not usable.

    Every target is a computed element, which a model may not claim, so this is
    the only thing that ever writes one.

    ``optional`` terms are simply left out. ``floor_when_missing`` terms are
    left out too, but their absence makes the result a FLOOR rather than the
    figure: an addend that could not be established can only push the sum up.
    NVIDIA has no finance leases at all, and requiring them made its adjusted
    debt unavailable — the analysis lost a figure because a real company had
    none of something. Treating the sum as "at least" says what is true without
    either blocking it or quietly presenting a partial total as a whole one.
    """
    values: dict[str, float] = {}
    missing: list[str] = []
    for key, sign in terms.items():
        element = elements.get(key)
        if element is not None and element.usable:
            values[key] = sign * (element.value or 0.0)
        elif key in floor_when_missing:
            missing.append(key)
        elif key not in optional:
            elements[target] = Element(
                target, None, "unavailable",
                blocked_reason=f"{key} is not usable, so everything built on it falls with it")
            return
    route = note or " ".join(f"{'+' if v >= 0 else '-'} {k}"
                             for k, v in values.items()).lstrip("+ ")
    contributions = tuple(Contribution(k, v) for k, v in values.items())
    if missing:
        elements[target] = Element(
            target, sum(values.values()), "lower_bound", route=route,
            contributions=contributions,
            blocked_reason=f"a floor, not the figure: {', '.join(missing)} could not be "
                           "established, and an addend that is missing can only raise the sum")
        return
    elements[target] = Element(target, sum(values.values()), "derived",
                               route=route, contributions=contributions)


__all__ = ["normalise"]
