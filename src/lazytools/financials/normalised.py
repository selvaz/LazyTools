"""The normalised financial base: what one agent hands the next.

Two agents meet here. One finds the figures in filings; the other analyses them.
Everything that goes wrong between them goes wrong because a number arrived
without the three things that make it usable: **where it came from**, **what it
covers**, and **whether anyone checked it**. So the base is not a dictionary of
floats. Every element carries its state, and a state of ``unavailable`` is an
answer — often a better one than a number nobody verified.

The registry below is also where each element's **economic meaning** lives, one
sentence each, machine-readable. That is deliberate: meaning written into prose
drifts from the code that computes the figure, while meaning written beside the
definition cannot.

What the base deliberately does NOT contain is ratios. Leverage and coverage
depend on thresholds, and thresholds are sector judgements. The base is the
shared, sector-neutral layer; the sector overlay consumes it and does not
overwrite it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

#: What is known about an element, in decreasing order of confidence.
#:
#: ``verified``      resolved and confirmed against its own parts.
#: ``derived``       computed from other elements; the formula is recorded.
#: ``reported``      taken from one source, nothing available to check it against.
#: ``lower_bound``   a part is missing, so the value is a floor and not the figure.
#: ``unreconciled``  resolved, but a check on it failed. Not usable as it stands.
#: ``unavailable``   no route produced it. An answer, not a gap to fill.
#: ``not_applicable`` the issuer's ontology gives this element no meaning.
ElementState = Literal[
    "verified", "derived", "reported", "lower_bound",
    "unreconciled", "unavailable", "not_applicable",
]

#: Elements a caller may not act on without deciding what to do about it.
BLOCKING_STATES: frozenset[str] = frozenset({"lower_bound", "unreconciled", "unavailable"})

Kind = Literal["duration", "instant"]
UnitKind = Literal["money", "per_share", "shares", "date"]


@dataclass(frozen=True)
class ElementSpec:
    """One element of the base: what it is, and what it means."""

    kind: Kind
    meaning: str
    unit_kind: UnitKind = "money"


#: The base. Sector-neutral by construction: an element is here because every
#: credit assessment of an ordinary corporate needs it, whatever the thresholds.
ELEMENTS: dict[str, ElementSpec] = {
    # --- what the business earns ------------------------------------------ #
    "revenue": ElementSpec("duration", "What the business sold. The denominator of every margin, and the first thing a perimeter change distorts."),
    "operating_income": ElementSpec("duration", "Profit after the cost of running the business but before financing and tax. Not EBIT: it excludes recurring non-operating items a literal EBIT would include."),
    "depreciation": ElementSpec("duration", "The charge for consuming physical assets. Removing it flatters an asset-heavy business and says nothing about the cash it must eventually spend again."),
    "amortisation_intangibles": ElementSpec("duration", "The charge for consuming acquired intangibles. Sits in cost of sales as well as operating expenses, so a single entity-wide figure is usually a slice."),
    "operating_da_total": ElementSpec("duration", "Depreciation and amortisation together, from ONE complete route. Mixing a combined tag with its own components double counts."),
    "impairment": ElementSpec("duration", "A write-down. Kept out of D&A on purpose: a filing that combines them makes an unusual charge look like a recurring one."),
    "house_operating_ebitda": ElementSpec("duration", "Operating income plus operating D&A. A house convention, not agency-adjusted EBITDA and not EBIT plus D&A — so agency thresholds do not apply to it."),

    # --- what financing costs --------------------------------------------- #
    "gross_interest_expense": ElementSpec("duration", "Interest as charged to profit. Differs from cash interest whenever there is capitalised, PIK or lease interest."),
    "cash_interest_paid": ElementSpec("duration", "Interest that actually left the business. The one that matters for whether it can pay."),
    "cash_taxes_paid": ElementSpec("duration", "Tax that actually left the business, which the tax charge often is not."),
    "house_ffo": ElementSpec("duration", "House operating EBITDA less cash interest and cash tax. Deliberately before working capital, and deliberately not called FFO — it does not reproduce an agency's adjustments."),

    # --- what turns into cash --------------------------------------------- #
    "cfo": ElementSpec("duration", "Cash from operations, after working capital. Where profit is tested against collection."),
    "capex_ppe": ElementSpec("duration", "Cash spent on physical assets. Rarely the whole capital programme."),
    "capex_intangibles": ElementSpec("duration", "Cash spent on intangibles and capitalised development. Omitting it makes free cash flow look larger than it is."),
    "house_capex": ElementSpec("duration", "The capital programme as counted here. Maintenance versus growth is a judgement the filings do not disclose, so no split is claimed."),
    "focf": ElementSpec("duration", "Cash left after keeping the business running, before distributions. Capacity to reduce debt from operations."),
    "dividends_paid": ElementSpec("duration", "Cash returned to shareholders as dividends."),
    "share_repurchases": ElementSpec("duration", "Cash returned through buybacks. Discretionary cash flow that omits it overstates what is left for creditors."),
    "dcf": ElementSpec("duration", "Free operating cash flow after dividends AND buybacks. What the business actually retains."),

    # --- what is owed ------------------------------------------------------ #
    "short_term_borrowings": ElementSpec("instant", "Borrowings due within a year that are not the current slice of long-term debt."),
    "current_long_term_debt": ElementSpec("instant", "The portion of long-term debt maturing within a year."),
    "long_term_debt_noncurrent": ElementSpec("instant", "Long-term debt beyond a year."),
    "reported_financial_debt": ElementSpec("instant", "The debt components summed, reconciled to any reported total. A 'combined' concept that does not reconcile is a subtotal wearing a total's name."),
    "finance_lease_total": ElementSpec("instant", "Finance lease liabilities. Frequently already inside reported debt, so adding them blindly double counts."),
    "operating_lease_total": ElementSpec("instant", "Operating lease liabilities, current and non-current together. One component alone is a floor, not the figure."),
    "house_adjusted_debt": ElementSpec("instant", "Debt as this house counts it, leases included. Meaningless without naming the convention: two agencies capitalise leases and one does not."),
    "cash_and_equivalents": ElementSpec("instant", "Cash on the balance sheet. Not the same as cash available to repay debt."),
    "restricted_cash": ElementSpec("instant", "Cash the issuer may not freely use. Subtracted only when disclosed; no percentage haircut is invented."),
    "readily_available_cash": ElementSpec("instant", "Cash less disclosed restrictions. Still says nothing about which legal entity holds it."),
    "house_net_debt": ElementSpec("instant", "Adjusted debt less readily available cash. Netting assumes the cash can reach the debt, which consolidation does not establish."),

    # --- when it comes due ------------------------------------------------- #
    "debt_maturity_y1": ElementSpec("instant", "Principal due within one year. The single most load-bearing liquidity figure."),
    "debt_maturity_y2": ElementSpec("instant", "Principal due in year two."),
    "debt_maturity_y3": ElementSpec("instant", "Principal due in year three."),
    "debt_maturity_y4": ElementSpec("instant", "Principal due in year four."),
    "debt_maturity_y5": ElementSpec("instant", "Principal due in year five."),
    "debt_maturity_thereafter": ElementSpec("instant", "Principal due beyond five years. A wall is visible only against the years before it."),
    "committed_facility": ElementSpec("instant", "Committed facility size. Not availability: drawings, letters of credit and borrowing bases reduce it."),
    "undrawn_availability": ElementSpec("instant", "What could actually be drawn. Uncommitted lines are not a liquidity source."),
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
    #: Named cross-checks and their outcome, e.g. ("debt components: balanced",).
    checks: tuple[str, ...] = ()
    #: Why there is no usable value. Required whenever the state is blocking.
    blocked_reason: str = ""

    def __post_init__(self) -> None:
        if self.id not in ELEMENTS:
            raise ValueError(f"{self.id!r} is not an element of the normalised base")
        if self.state in BLOCKING_STATES and not self.blocked_reason:
            raise ValueError(
                f"{self.id}: state {self.state!r} needs a blocked_reason — a caller that "
                "cannot use this figure has to be told what would make it usable"
            )
        if self.state in ("verified", "derived", "reported") and self.value is None:
            raise ValueError(f"{self.id}: state {self.state!r} claims a value and has none")

    @property
    def usable(self) -> bool:
        return self.state in ("verified", "derived", "reported")

    @property
    def spec(self) -> ElementSpec:
        return ELEMENTS[self.id]


@dataclass(frozen=True)
class NormalisedBase:
    """One issuer, one period, normalised — the handover between the agents.

    Carries no ratios. Leverage and coverage need thresholds, thresholds are
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

    def get(self, element_id: str) -> Element | None:
        return self.elements.get(element_id)

    def value(self, element_id: str) -> float | None:
        """The value only if it is usable — otherwise ``None``.

        Deliberately collapses "unavailable" and "unreconciled" for arithmetic:
        a caller doing sums must not silently consume a figure that failed its
        own check. To tell the two apart, read the element.
        """
        element = self.elements.get(element_id)
        return element.value if element and element.usable else None

    def blocked(self) -> tuple[Element, ...]:
        """Every element a caller cannot act on, with its reason."""
        return tuple(e for e in self.elements.values() if e.state in BLOCKING_STATES)

    def missing(self) -> tuple[str, ...]:
        """Elements of the base that were never populated at all.

        Distinct from ``unavailable``: an element nobody attempted is not an
        element that was looked for and not found.
        """
        return tuple(k for k in ELEMENTS if k not in self.elements)

    def to_dict(self) -> dict[str, Any]:
        """The wire form. This is the interface, so it has to serialise."""
        return {
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
                    "sources": list(e.sources), "checks": list(e.checks),
                    "blocked_reason": e.blocked_reason,
                    "meaning": e.spec.meaning, "kind": e.spec.kind,
                }
                for key, e in self.elements.items()
            },
        }


__all__ = [
    "BLOCKING_STATES",
    "ELEMENTS",
    "Element",
    "ElementSpec",
    "ElementState",
    "NormalisedBase",
]
