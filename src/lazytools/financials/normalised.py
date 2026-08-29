"""The normalised financial base: what one agent hands the next.

Two agents meet here. One finds the figures in filings; the other analyses them.
Everything that goes wrong between them goes wrong because a number arrives
without the three things that make it usable: **where it came from**, **what it
covers**, and **whether anyone checked it**. So the base is not a dictionary of
floats, and it refuses at construction the shapes that would let an unusable
figure travel as a usable one — including the one where the key says ``revenue``
and the element inside it is cash flow.

A state is a claim, and every claim has to be paid for. ``reported`` needs a
source, ``derived`` needs its formula, ``verified`` needs a check that passed,
and a check that FAILED forbids any usable state at all — a figure cannot be
verified and contradicted at the same time.

The registry is also where each element's **economic meaning** lives, one
sentence each, machine-readable. Meaning written into prose drifts from the code
that computes the figure; meaning written beside the definition cannot.

What the base deliberately does NOT contain is ratios. Leverage and coverage
depend on thresholds, thresholds are sector judgements, and the sector overlay
consumes this layer rather than replacing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Any, Literal, get_args

#: Bumped whenever an element id, a state or a meaning changes, so a consumer
#: reading an older payload can refuse it rather than misread it.
#:
#: It also retires every cached statement mapping, which is the point rather
#: than a side effect: a mapping answers "which of THESE elements is which
#: line", so a registry that has gained an element makes every stored answer an
#: answer to a different question — silently missing the new one.
#:
#: 2: added ``lease_fleet_investment``. Equipment bought to lease out was
#:    arriving as ``capex_intangibles``.
#: 3: ``revenue`` now asks for the TOTAL rather than a revenue subtotal.
#:    Walmart was mapping to 'Net sales', which omits membership income.
SCHEMA_VERSION = 3

#: What is known about an element, in decreasing order of confidence.
#:
#: ``verified``      resolved and confirmed by a check that passed.
#: ``derived``       computed from other elements; the formula is recorded.
#: ``reported``      taken from a named source, with nothing to check it against.
#: ``lower_bound``   a part is missing, so this is a floor and not the figure.
#: ``unreconciled``  resolved, but a check on it failed. Not usable as it stands.
#: ``unavailable``   looked for and not found. An answer, not a gap to fill.
#: ``not_applicable`` the issuer has none — distinct from failing to find it.
ElementState = Literal[
    "verified", "derived", "reported", "lower_bound",
    "unreconciled", "unavailable", "not_applicable",
]
_STATES: frozenset[str] = frozenset(get_args(ElementState))
#: States a caller may not act on without deciding what to do about it.
BLOCKING_STATES: frozenset[str] = frozenset({"lower_bound", "unreconciled", "unavailable"})
#: States that assert a figure and therefore owe evidence for it.
USABLE_STATES: frozenset[str] = frozenset({"verified", "derived", "reported"})
#: States that assert the ABSENCE of a figure and must not carry one.
EMPTY_STATES: frozenset[str] = frozenset({"unavailable", "not_applicable"})

Kind = Literal["duration", "instant"]
#: How an element's sign is to be read.
#:
#: ``magnitude``  an amount whose sign carries no information: capex, dividends
#:                and taxes paid are outflows however the filer presents them,
#:                and a rendered cash-flow statement shows them negative while an
#:                XBRL fact shows the same figure positive. Stored as a positive
#:                magnitude so that anything summing them can state its own signs.
#: ``signed``     an element whose sign IS information: operating income, free
#:                cash flow and a working-capital movement are all legitimately
#:                negative, and forcing them positive would destroy the finding.
Sign = Literal["magnitude", "signed"]
#: Where an element may come from.
#:
#: ``presented``  a line a filer actually shows. Mappable from a statement.
#: ``computed``   a figure only this code produces, from other elements. A model
#:                may NOT claim one: a presented line accepted as free cash flow
#:                silently replaces the calculation with a relabelled cash-flow
#:                line, which is exactly what happened before this existed.
Origin = Literal["presented", "computed"]


@dataclass(frozen=True)
class ElementSpec:
    """One element of the base: what it is, and what it means."""

    kind: Kind
    meaning: str
    sign: Sign = "signed"
    origin: Origin = "presented"


@dataclass(frozen=True)
class Check:
    """One named cross-check and whether it passed.

    A free-form note would let "components: failed" sit beside a state of
    ``verified``. An outcome that the constructor can read cannot.
    """

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class Contribution:
    """One signed input to a derived figure or one applied adjustment.

    A route string says a figure was adjusted; it does not let anyone reproduce
    it. These do: element or reference, signed amount, and why.
    """

    source: str
    amount: float
    reason: str = ""


#: The base. Sector-neutral by construction: an element is here because every
#: credit assessment of an ordinary corporate needs it, whatever the thresholds.
#: Maturity buckets are measured in months from the balance-sheet date, as the
#: issuer disclosed them, so two producers cannot encode different periods.
ELEMENTS: dict[str, ElementSpec] = {
    # --- what the business earns ------------------------------------------ #
    "revenue": ElementSpec("duration", "TOTAL revenue for the period, on the issuer's own recognition policy. Where a filer shows a revenue subtotal and a total - 'Net sales' above 'Total revenues' - take the TOTAL: the subtotal omits income the business really earned.", sign="magnitude"),
    "operating_income": ElementSpec("duration", "Profit after the cost of running the business, before financing and tax. Not EBIT: what an issuer places above this line varies, and recurring non-operating items may sit outside it."),
    "depreciation": ElementSpec("duration", "The charge for consuming physical assets. A non-cash charge, but a business that keeps operating must eventually spend the cash again.", sign="magnitude"),
    "amortisation_intangibles": ElementSpec("duration", "The charge for consuming acquired intangibles. Can be split across cost of sales and operating expenses, so a figure from one location is not the total.", sign="magnitude"),
    "operating_da_total": ElementSpec("duration", "Depreciation and amortisation together, from ONE complete route. A combined figure summed with its own components double counts.", sign="magnitude"),
    "impairment": ElementSpec("duration", "A write-down of asset carrying value. Held apart from D&A: a filing that combines them makes a one-off look recurring.", sign="magnitude"),
    "house_operating_ebitda": ElementSpec("duration", "Operating income plus operating D&A. A house convention: it reproduces no agency's adjustments, so no agency's thresholds apply to it.", origin="computed"),

    # --- what financing costs --------------------------------------------- #
    "gross_interest_expense": ElementSpec("duration", "Interest charged to profit. Differs from cash interest by capitalised, PIK and lease interest.", sign="magnitude"),
    "cash_interest_paid": ElementSpec("duration", "Interest that actually left the business in the period, which is the test of whether it can be paid.", sign="magnitude"),
    "cash_taxes_paid": ElementSpec("duration", "Tax that left the business in the period, which the tax charge often is not.", sign="magnitude"),
    "house_ffo": ElementSpec("duration", "House operating EBITDA less cash interest and cash tax, before working capital. Not an agency's FFO, and named so it cannot be mistaken for one.", origin="computed"),

    # --- what turns into cash --------------------------------------------- #
    "cfo": ElementSpec("duration", "Cash generated by operations, after working capital, interest and tax as the issuer classifies them."),
    "working_capital_movement": ElementSpec("duration", "The change in operating working capital inside CFO. Without it the gap between earnings and cash cannot be explained, only observed."),
    "capex_ppe": ElementSpec("duration", "Cash paid for property, plant and equipment. Rarely the whole capital programme: intangibles and lease-financed assets sit elsewhere.", sign="magnitude"),
    "capex_intangibles": ElementSpec("duration", "Cash paid for INTANGIBLES ONLY - software, capitalised development, acquired rights. Not equipment of any kind. Excluded from a capex figure, it inflates free cash flow.", sign="magnitude"),
    "lease_fleet_investment": ElementSpec("duration", "Cash paid for EQUIPMENT ACQUIRED TO LEASE OUT to customers - a lessor's or captive finance arm's fleet. Real capital spending, but not the industrial business's own.", sign="magnitude"),
    "house_capex": ElementSpec("duration", "The capital spend counted here. No maintenance/growth split is claimed: filings do not disclose one.", sign="magnitude", origin="computed"),
    "focf": ElementSpec("duration", "Operating cash flow less capital spend. Capacity to repay debt from operations, before any distribution.", origin="computed"),
    "dividends_paid": ElementSpec("duration", "Cash paid to shareholders as dividends. Discretionary in law and rarely in practice, which is why it is deducted before residual cash.", sign="magnitude"),
    "share_repurchases": ElementSpec("duration", "Cash paid to shareholders through buybacks. Omitting it from residual cash overstates what is left for creditors.", sign="magnitude"),
    "cash_acquisitions": ElementSpec("duration", "Cash paid for businesses. Excluded from residual cash, an acquisitive issuer looks like it retained what it spent.", sign="magnitude"),
    "cash_divestiture_proceeds": ElementSpec("duration", "Cash received from disposals. Distinguishes deleveraging by selling from deleveraging by earning.", sign="magnitude"),
    "dcf": ElementSpec("duration", "Free operating cash flow after dividends and buybacks. Retained cash BEFORE acquisitions and disposals, which are reported separately.", origin="computed"),

    # --- what is owed ------------------------------------------------------ #
    "short_term_borrowings": ElementSpec("instant", "Borrowings due within a year other than the current portion of long-term debt.", sign="magnitude"),
    "current_long_term_debt": ElementSpec("instant", "The portion of long-term debt maturing within a year.", sign="magnitude"),
    "long_term_debt_noncurrent": ElementSpec("instant", "Long-term debt maturing beyond twelve months of the balance-sheet date.", sign="magnitude"),
    "reported_financial_debt": ElementSpec("instant", "The debt components summed and reconciled against any reported total. A total that reconciles only to some components is a subtotal.", sign="magnitude"),
    "finance_lease_current": ElementSpec("instant", "Finance lease liabilities falling due within twelve months.", sign="magnitude"),
    "finance_lease_noncurrent": ElementSpec("instant", "Finance lease liabilities falling due beyond twelve months.", sign="magnitude"),
    "finance_lease_total": ElementSpec("instant", "Finance lease liabilities, current and non-current together. Often already inside reported debt, where adding them again double counts.", sign="magnitude", origin="computed"),
    "operating_lease_current": ElementSpec("instant", "Operating lease liabilities falling due within twelve months.", sign="magnitude"),
    "operating_lease_noncurrent": ElementSpec("instant", "Operating lease liabilities falling due beyond twelve months.", sign="magnitude"),
    "operating_lease_total": ElementSpec("instant", "Operating lease liabilities, current and non-current together. A model that reads the current line alone reports a fifth of a retailer's lease debt as all of it.", sign="magnitude", origin="computed"),
    "house_adjusted_debt": ElementSpec("instant", "Debt as this house counts it. The lease convention must be stated: two major agencies capitalise leases and one does not, so the figures are not comparable across conventions.", sign="magnitude", origin="computed"),
    "cash_and_equivalents": ElementSpec("instant", "Cash and equivalents free of disclosed restrictions. Being on the balance sheet is not the same as being available to the entity that owes the debt.", sign="magnitude"),
    "cash_plus_restricted": ElementSpec("instant", "Cash including restricted amounts, as some issuers report only this. Recorded separately because subtracting restrictions from the wrong one of the two mis-states available cash in either direction.", sign="magnitude"),
    "restricted_cash": ElementSpec("instant", "Cash the issuer may not freely use, where disclosed. No percentage haircut is applied in its absence.", sign="magnitude"),
    "readily_available_cash": ElementSpec("instant", "Cash less disclosed restrictions. Says nothing about which legal entity holds it or whether it can reach the debt.", sign="magnitude", origin="computed"),
    "house_net_debt": ElementSpec("instant", "Adjusted debt less readily available cash. Netting assumes the cash can reach the debt, which consolidation does not establish.", origin="computed"),

    # --- when it comes due ------------------------------------------------- #
    "debt_maturity_y1": ElementSpec("instant", "Principal falling due within 12 months after the balance-sheet date. The single most load-bearing liquidity figure.", sign="magnitude"),
    "debt_maturity_y2": ElementSpec("instant", "Principal falling due 13-24 months after the balance-sheet date, as the issuer bucketed it.", sign="magnitude"),
    "debt_maturity_y3": ElementSpec("instant", "Principal falling due 25-36 months after the balance-sheet date, as the issuer bucketed it.", sign="magnitude"),
    "debt_maturity_y4": ElementSpec("instant", "Principal falling due 37-48 months after the balance-sheet date, as the issuer bucketed it.", sign="magnitude"),
    "debt_maturity_y5": ElementSpec("instant", "Principal falling due 49-60 months after the balance-sheet date, as the issuer bucketed it.", sign="magnitude"),
    "debt_maturity_thereafter": ElementSpec("instant", "Principal due beyond 60 months. A maturity wall is visible only against the years before it.", sign="magnitude"),
    "committed_facility": ElementSpec("instant", "Committed facility size. Not availability: drawings, letters of credit and borrowing-base tests reduce it.", sign="magnitude"),
    "undrawn_availability": ElementSpec("instant", "What could actually be drawn. Uncommitted lines are not a liquidity source.", sign="magnitude"),
}


@dataclass(frozen=True)
class Element:
    """One normalised figure, with everything needed to trust or refuse it."""

    id: str
    value: float | None
    state: ElementState
    #: How it was obtained: a concept, a formula, or the statement it was read from.
    route: str = ""
    #: Where it came from, precisely enough to be re-found.
    sources: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()
    #: The signed inputs behind a derived figure or an applied adjustment.
    contributions: tuple[Contribution, ...] = ()
    #: Why there is no usable value. Required whenever the state is blocking.
    blocked_reason: str = ""

    def __post_init__(self) -> None:
        if self.id not in ELEMENTS:
            raise ValueError(f"{self.id!r} is not an element of the normalised base")
        if self.state not in _STATES:
            raise ValueError(f"{self.id}: {self.state!r} is not a state; expected one of {sorted(_STATES)}")
        if self.failed_checks and self.state in USABLE_STATES:
            raise ValueError(
                f"{self.id}: a check failed ({self.failed_checks[0].name}) but the state is "
                f"{self.state!r} — a figure cannot be usable and contradicted at once"
            )
        if self.state in BLOCKING_STATES and not self.blocked_reason:
            raise ValueError(
                f"{self.id}: state {self.state!r} needs a blocked_reason — a caller that "
                "cannot use this figure has to be told what would make it usable"
            )
        if self.state in EMPTY_STATES and self.value is not None:
            raise ValueError(f"{self.id}: state {self.state!r} asserts no figure and carries one")
        if self.state in USABLE_STATES and self.value is None:
            raise ValueError(f"{self.id}: state {self.state!r} claims a value and has none")
        # A state is a claim, and every claim is paid for.
        if self.state == "reported" and not self.sources:
            raise ValueError(f"{self.id}: 'reported' needs a source naming where it was read")
        if self.state == "derived" and not (self.route or self.contributions):
            raise ValueError(f"{self.id}: 'derived' needs the formula or the inputs it came from")
        if self.state == "verified" and not any(c.passed for c in self.checks):
            raise ValueError(f"{self.id}: 'verified' needs a check that passed")

    @property
    def usable(self) -> bool:
        return self.state in USABLE_STATES

    @property
    def failed_checks(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def spec(self) -> ElementSpec:
        return ELEMENTS[self.id]


@dataclass(frozen=True)
class NormalisedBase:
    """One issuer, one period, normalised — the handover between the agents.

    Carries no ratios: leverage and coverage need thresholds, thresholds are
    sector judgements, and the sector overlay consumes this layer rather than
    replacing it.
    """

    issuer_name: str
    cik: str
    ontology: str
    #: Structural signals the ontology gate raised and that are still open.
    open_signals: tuple[str, ...]
    accounting_standard: str
    currency: str
    period_start: date | None
    period_end: date
    #: Nothing filed after this date was used, so the base is reproducible.
    information_cutoff: date
    #: ``matched``, ``reported_but_mismatched`` or ``unavailable``. A period
    #: spanning a material acquisition puts full debt against partial earnings.
    perimeter_status: str
    accession: str
    elements: dict[str, Element] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for key, element in self.elements.items():
            if key != element.id:
                raise ValueError(
                    f"element filed under {key!r} carries id {element.id!r} — the key and "
                    "the figure must be the same element, or a reader asks for one and "
                    "gets the other"
                )
        # frozen= does not freeze a dict; a handover that can be edited after it
        # was validated has not been validated.
        object.__setattr__(self, "elements", MappingProxyType(dict(self.elements)))

    def get(self, element_id: str) -> Element | None:
        return self.elements.get(element_id)

    def value(self, element_id: str) -> float | None:
        """The value only if it is usable — otherwise ``None``.

        A figure that failed its own check cannot be summed by accident. To act
        on a lower bound deliberately, use :meth:`lower_bound`.
        """
        element = self.elements.get(element_id)
        return element.value if element and element.usable else None

    def lower_bound(self, element_id: str) -> float | None:
        """A figure usable for an "at least X" claim, and nothing stronger.

        Separate from :meth:`value` on purpose: reaching a floor should be a
        decision a reader can see in the calling code, not a silent widening of
        what counts as a number.
        """
        element = self.elements.get(element_id)
        if element is None:
            return None
        return element.value if element.usable or element.state == "lower_bound" else None

    def blocked(self) -> tuple[Element, ...]:
        """Every element a caller cannot act on, with its reason."""
        return tuple(e for e in self.elements.values() if e.state in BLOCKING_STATES)

    def missing(self) -> tuple[str, ...]:
        """Registry elements the producer never populated at all.

        Distinct from ``unavailable``: an element nobody attempted is not one
        that was looked for and not found, and only the second is a finding
        about the issuer.
        """
        return tuple(k for k in ELEMENTS if k not in self.elements)

    def to_dict(self) -> dict[str, Any]:
        """The wire form. This is the interface, so it has to serialise."""
        return {
            "schema_version": SCHEMA_VERSION,
            "issuer_name": self.issuer_name,
            "cik": self.cik,
            "ontology": self.ontology,
            "open_signals": list(self.open_signals),
            "accounting_standard": self.accounting_standard,
            "currency": self.currency,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat(),
            "information_cutoff": self.information_cutoff.isoformat(),
            "perimeter_status": self.perimeter_status,
            "accession": self.accession,
            "elements": {
                key: {
                    "value": e.value, "state": e.state, "route": e.route,
                    "sources": list(e.sources),
                    "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                               for c in e.checks],
                    "contributions": [{"source": c.source, "amount": c.amount, "reason": c.reason}
                                      for c in e.contributions],
                    "blocked_reason": e.blocked_reason,
                    "meaning": e.spec.meaning, "kind": e.spec.kind,
                }
                for key, e in self.elements.items()
            },
        }


COMPUTED_ELEMENTS: frozenset[str] = frozenset(
    k for k, spec in ELEMENTS.items() if spec.origin == "computed")


__all__ = [
    "BLOCKING_STATES",
    "COMPUTED_ELEMENTS",
    "ELEMENTS",
    "EMPTY_STATES",
    "SCHEMA_VERSION",
    "USABLE_STATES",
    "Check",
    "Contribution",
    "Element",
    "ElementSpec",
    "ElementState",
    "NormalisedBase",
]
