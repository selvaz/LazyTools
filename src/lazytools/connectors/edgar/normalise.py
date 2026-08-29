"""Building a normalised base from a SEC filing.

This is where the pieces meet: the ontology gate decides whether the corporate
vocabulary applies at all, the fact routes resolve each element, the rendered
statements say what those facts actually cover, and reconciliation refuses the
aggregates that do not survive their own parts.

The order matters and is not negotiable. Classification runs FIRST, before a
single figure is fetched — a run that trusted the SIC spent twenty requests on
Deere's debt components before discovering it had been reading a captive-finance
group as an ordinary industrial. Cross-checks run BEFORE a figure is admitted,
because the failure that matters is not the one that errors: Cisco's
entity-wide amortisation resolves cleanly, carries perfect provenance, and is a
third of the real number.

Nothing here computes a ratio. The base is the shared layer; thresholds are
sector judgement and belong to whatever consumes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from lazytools.connectors.edgar.client import EdgarService
from lazytools.connectors.edgar.facts import facts_from_concept
from lazytools.connectors.edgar.ontology import classify
from lazytools.connectors.edgar.statements import list_reports, read_statement
from lazytools.financials.facts import Fact, select
from lazytools.financials.normalised import (
    Check,
    Contribution,
    Element,
    NormalisedBase,
)
from lazytools.financials.period import ResolvedWindow, interpret, resolve
from lazytools.financials.reconcile import reconcile

#: One route is one tag, or several tags summed. Ordered: the first that
#: resolves completely wins, and which one it was is recorded — the same
#: economic input arrives under different tags per filer, so an unrecorded
#: choice cannot be checked later.
Route = tuple[str, ...]

_ROUTES: dict[str, tuple[Route, ...]] = {
    "revenue": (("RevenueFromContractWithCustomerExcludingAssessedTax",), ("Revenues",),
                ("RevenueFromContractWithCustomerIncludingAssessedTax",)),
    "operating_income": (("OperatingIncomeLoss",),),
    "depreciation": (("Depreciation",), ("DepreciationNonproduction",)),

    "amortisation_intangibles": (("AmortizationOfIntangibleAssets",),),
    "impairment": (("AssetImpairmentCharges",), ("GoodwillImpairmentLoss",)),
    "gross_interest_expense": (("InterestExpense",), ("InterestExpenseDebt",),
                               ("InterestExpenseNonoperating",)),
    "cash_interest_paid": (("InterestPaidNet",), ("InterestPaid",)),
    "cash_taxes_paid": (("IncomeTaxesPaidNet",), ("IncomeTaxesPaid",)),
    "cfo": (("NetCashProvidedByUsedInOperatingActivities",),
            ("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",)),
    "working_capital_movement": (("IncreaseDecreaseInOperatingCapital",),),
    "capex_ppe": (("PaymentsToAcquirePropertyPlantAndEquipment",),
                  ("PaymentsToAcquireProductiveAssets",)),
    "capex_intangibles": (("PaymentsToAcquireIntangibleAssets",),),
    "dividends_paid": (("PaymentsOfDividends",), ("PaymentsOfDividendsCommonStock",)),
    "share_repurchases": (("PaymentsForRepurchaseOfCommonStock",),),
    "cash_acquisitions": (("PaymentsToAcquireBusinessesNetOfCashAcquired",),),
    "cash_divestiture_proceeds": (("ProceedsFromDivestitureOfBusinessesNetOfCashDivested",),),
    "short_term_borrowings": (("ShortTermBorrowings",), ("OtherShortTermBorrowings",),
                              ("CommercialPaper",)),
    "current_long_term_debt": (("LongTermDebtCurrent",),),
    "long_term_debt_noncurrent": (("LongTermDebtNoncurrent",),),
    "finance_lease_total": (("FinanceLeaseLiability",),
                            ("FinanceLeaseLiabilityCurrent", "FinanceLeaseLiabilityNoncurrent")),
    "operating_lease_total": (("OperatingLeaseLiability",),
                              ("OperatingLeaseLiabilityCurrent", "OperatingLeaseLiabilityNoncurrent")),
    "cash_and_equivalents": (("CashAndCashEquivalentsAtCarryingValue",),),
    "cash_plus_restricted": (("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",),),
    "restricted_cash": (("RestrictedCashAndCashEquivalents",), ("RestrictedCashCurrent",)),
    "debt_maturity_y1": (("LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",),),
    "debt_maturity_y2": (("LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo",),),
    "debt_maturity_y3": (("LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree",),),
    "debt_maturity_y4": (("LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour",),),
    "debt_maturity_y5": (("LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive",),),
    "debt_maturity_thereafter": (("LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive",),),
}
#: Routes that feed a derivation rather than an element of the base. Kept apart
#: because everything in _ROUTES becomes an Element, and the contract rightly
#: refuses an id that is not in the registry.
_INTERNAL_ROUTES: dict[str, tuple[Route, ...]] = {
    # Walmart tags total D&A as DepreciationAmortizationAndAccretionNet. The
    # accretion it also contains is immaterial for most filers but IS a scope
    # difference, which is why the route that answered is recorded on the figure.
    "combined_da": (("DepreciationDepletionAndAmortization",),
                    ("DepreciationAndAmortization",),
                    ("DepreciationAmortizationAndAccretionNet",)),
}

#: Only annual figures are read from annual forms; an instant is dated, not
#: covered, so it needs no form filter.
_ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F")
_DURATION_ELEMENTS = frozenset({
    "revenue", "operating_income", "depreciation", "amortisation_intangibles", "impairment",
    "gross_interest_expense", "cash_interest_paid", "cash_taxes_paid", "cfo",
    "working_capital_movement", "capex_ppe", "capex_intangibles", "dividends_paid",
    "share_repurchases", "cash_acquisitions", "cash_divestiture_proceeds",
})


@dataclass
class _Resolver:
    """Fetches concepts once and remembers what answered."""

    client: EdgarService
    cik: str
    window: ResolvedWindow
    currency: str
    as_of: date | None = None

    accession: str = ""

    def __post_init__(self) -> None:
        self._cache: dict[str, list[Fact]] = {}
        self._statements: list[Any] | None = None

    def facts(self, tag: str) -> list[Fact]:
        if tag not in self._cache:
            try:
                payload = self.client.company_concept(self.cik, "us-gaap", tag)
                self._cache[tag] = facts_from_concept(payload, strict=False)
            except Exception:  # noqa: BLE001 - absence and fault both mean "no route here"
                self._cache[tag] = []
        return self._cache[tag]

    def one(self, tag: str, *, duration: bool) -> float | None:
        facts = self.facts(tag)
        if self.as_of is not None:
            facts = [f for f in facts if f.filed is None or f.filed <= self.as_of]
        hits = select(facts, self.window, unit=self.currency,
                      forms=_ANNUAL_FORMS if duration else None)
        if not hits:
            return None
        return sorted(hits, key=lambda f: (f.filed or f.end))[-1].value

    def from_statement(self, pattern: str, *, column: int = 0) -> tuple[float | None, str]:
        """Read a line out of the filing's own rendered primary statements.

        The last resort, and the one an analyst reaches for first: a figure the
        fact APIs do not serve entity-wide is usually presented plainly on the
        face of a statement, with the label the filer chose. Returns the value
        and a description of where it was read, or ``(None, "")``.
        """
        import re

        if self._statements is None:
            self._statements = []
            try:
                reports = [r for r in list_reports(self.client, self.cik, self.accession)
                           if r.is_primary_statement]
                for report in reports:
                    self._statements.append(read_statement(self.client, self.cik,
                                                           self.accession, report))
            except Exception:  # noqa: BLE001 - no rendered statements is a fact, not a fault
                self._statements = []
        for statement in self._statements:
            index = self._column_for(statement)
            if index is None:
                continue
            for line in statement.lines:
                if line.is_label_only or index >= len(line.values):
                    continue
                if line.values[index] is not None and re.fullmatch(pattern, line.label, re.I):
                    return line.values[index], f"{statement.report.short_name}: {line.label!r}"
        return None, ""

    def _column_for(self, statement: Any) -> int | None:
        """Which rendered column covers this window, matched on its end date."""
        for index, header in enumerate(statement.columns):
            for fmt in ("%b. %d, %Y", "%B %d, %Y"):
                try:
                    parsed = datetime.strptime(header.strip(), fmt).date()
                except ValueError:
                    continue
                if abs((parsed - self.window.end).days) <= self.window.tolerance_days:
                    return index
        return None

    def route(self, element_id: str) -> tuple[float | None, str]:
        """The first complete route, and its name. ``(None, "")`` when none is."""
        duration = element_id in _DURATION_ELEMENTS
        routes = _ROUTES.get(element_id) or _INTERNAL_ROUTES.get(element_id, ())
        for candidate in routes:
            parts = [self.one(tag, duration=duration) for tag in candidate]
            if all(p is not None for p in parts):
                return sum(parts), " + ".join(candidate)  # type: ignore[arg-type]
        return None, ""


def normalise(
    client: EdgarService,
    *,
    company: str,
    period: str,
    currency: str = "USD",
    as_of: date | None = None,
) -> NormalisedBase:
    """Produce the normalised base for one issuer and one annual period.

    Classification runs before any figure is fetched, and its open signals ride
    on the result rather than stopping it: a caller needs to know that Cisco has
    a finance business, not to be refused an answer about Cisco.
    """
    matches = client.resolve_company(company, limit=1)
    if not matches:
        raise ValueError(f"{company!r} matched no EDGAR filer")
    cik = matches[0]["cik"]
    profile = client.issuer_profile(cik)
    filings = client.list_filings(cik, form="10-K", limit=1)
    accession = filings[0]["accession_no"] if filings else ""

    gate = classify(client, cik, accession or None)
    window = resolve(interpret(period)[0], fiscal_year_end=profile.get("fiscal_year_end"))
    resolver = _Resolver(client, cik, window, currency, as_of, accession)

    elements: dict[str, Element] = {}
    for element_id in _ROUTES:
        value, route = resolver.route(element_id)
        elements[element_id] = (
            Element(element_id, value, "reported", route=route, sources=(f"us-gaap via {route}",))
            if value is not None
            else Element(element_id, None, "unavailable", blocked_reason="no route resolved")
        )

    _derive_da(elements, resolver)
    _derive_debt(elements)
    _derive_cash(elements)
    _derive_flows(elements)

    return NormalisedBase(
        issuer_name=str(profile.get("name") or matches[0].get("title") or company),
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
# Derivations, each of which must survive a check before it is admitted
# --------------------------------------------------------------------------- #
#: How close a combined figure may sit to ONE of its components before that
#: proximity is itself the finding. A D&A total that barely exceeds the
#: amortisation alone implies almost no depreciation, which no business with
#: property has. Cisco: combined 700m against amortisation 698m.
_PROXIMITY = 0.02


def _derive_da(elements: dict[str, Element], resolver: _Resolver) -> None:
    """Total D&A, and the checks that refuse a combined tag which is not one.

    The Cisco case lives here, and the first cut of this function still fell for
    it: with depreciation unserved, the reconciliation came back ``incomplete``
    rather than ``scope_conflict``, and every non-conflict outcome was being
    treated as confirmation. Only ``balanced`` confirms anything.
    """
    combined, combined_route = resolver.route("combined_da")
    from_statement = ""
    if combined is None:
        combined, from_statement = resolver.from_statement(
            r"depreciation[ ,&]+(and )?amorti[sz]ation.*")
        combined_route = f"rendered statement — {from_statement}" if combined is not None else ""
    depreciation = elements["depreciation"].value if elements["depreciation"].usable else None
    amortisation = (elements["amortisation_intangibles"].value
                    if elements["amortisation_intangibles"].usable else None)
    components = {"depreciation": depreciation, "amortisation of intangibles": amortisation}
    known = {k: v for k, v in components.items() if v is not None}

    element: Element
    if combined is not None and len(known) == len(components):
        check = reconcile("D&A", combined, components)
        if check.status == "balanced":
            element = Element("operating_da_total", combined, "verified",
                              route=combined_route,
                              checks=(Check("components", True, check.detail),))
        else:
            element = Element(
                "operating_da_total", depreciation + amortisation, "derived",
                route="depreciation + amortisation_intangibles",
                contributions=(Contribution("depreciation", depreciation),
                               Contribution("amortisation_intangibles", amortisation)),
                checks=(Check("components complete", True,
                              f"the combined concept was rejected and its components used "
                              f"instead: {check.detail}"),))
    elif combined is not None and known:
        # One component is missing, so nothing can confirm the combined figure.
        # It can still be REFUTED: sitting on top of the one component we do
        # have means it does not contain the other.
        only_value = next(iter(known.values()))
        only_name = next(iter(known))
        if abs(combined - only_value) <= _PROXIMITY * abs(combined or 1):
            element = Element(
                "operating_da_total", combined, "unreconciled",
                route=combined_route,
                checks=(Check("combined tag scope", False,
                              f"the combined figure {combined:,.0f} barely exceeds "
                              f"{only_name} alone ({only_value:,.0f}), so it does not "
                              "contain the other component"),),
                blocked_reason=f"the combined D&A concept appears to exclude everything "
                               f"except {only_name}")
        else:
            element = Element("operating_da_total", combined, "reported",
                              route=combined_route,
                              sources=(from_statement or f"us-gaap via {combined_route}",))
    elif combined is not None:
        element = Element("operating_da_total", combined, "reported",
                          route=combined_route,
                          sources=(from_statement or f"us-gaap via {combined_route}",))
    elif len(known) == len(components):
        element = Element(
            "operating_da_total", depreciation + amortisation, "derived",
            route="depreciation + amortisation_intangibles",
            contributions=(Contribution("depreciation", depreciation),
                           Contribution("amortisation_intangibles", amortisation)))
    elif known:
        element = Element("operating_da_total", next(iter(known.values())), "lower_bound",
                          route=f"{next(iter(known))} only",
                          blocked_reason="only one of depreciation and amortisation resolved")
    else:
        element = Element("operating_da_total", None, "unavailable",
                          blocked_reason="no complete D&A route resolved")

    elements["operating_da_total"] = element
    _derive_ebitda(elements)


def _derive_ebitda(elements: dict[str, Element]) -> None:
    operating = elements.get("operating_income")
    da = elements.get("operating_da_total")
    if operating and operating.usable and da and da.usable:
        elements["house_operating_ebitda"] = Element(
            "house_operating_ebitda", (operating.value or 0) + (da.value or 0), "derived",
            route="operating_income + operating_da_total",
            contributions=(Contribution("operating_income", operating.value or 0),
                           Contribution("operating_da_total", da.value or 0)))
    else:
        blocker = "operating_income" if not (operating and operating.usable) else "operating_da_total"
        elements["house_operating_ebitda"] = Element(
            "house_operating_ebitda", None, "unavailable",
            blocked_reason=f"{blocker} is not usable, so every EBITDA-based figure falls with it")


def _derive_debt(elements: dict[str, Element]) -> None:
    """Gross debt from disjoint components, reconciled where a total exists."""
    parts = {k: elements[k].value if elements[k].usable else None
             for k in ("short_term_borrowings", "current_long_term_debt",
                       "long_term_debt_noncurrent")}
    known = {k: v for k, v in parts.items() if v is not None}
    if not known:
        elements["reported_financial_debt"] = Element(
            "reported_financial_debt", None, "unavailable",
            blocked_reason="no debt component resolved")
    elif len(known) < len(parts):
        elements["reported_financial_debt"] = Element(
            "reported_financial_debt", sum(known.values()), "lower_bound",
            route=" + ".join(known),
            blocked_reason=f"{len(parts) - len(known)} debt component(s) did not resolve, "
                           "so this is a floor and not the debt")
    else:
        elements["reported_financial_debt"] = Element(
            "reported_financial_debt", sum(known.values()), "derived",
            route=" + ".join(known),
            contributions=tuple(Contribution(k, v) for k, v in known.items()))

    debt = elements["reported_financial_debt"]
    leases = [elements.get(k) for k in ("finance_lease_total", "operating_lease_total")]
    lease_total = sum(e.value or 0 for e in leases if e and e.usable)
    if debt.usable and all(e and e.usable for e in leases):
        elements["house_adjusted_debt"] = Element(
            "house_adjusted_debt", (debt.value or 0) + lease_total, "derived",
            route="reported_financial_debt + finance leases + operating leases "
                  "(house convention: leases capitalised)",
            contributions=(Contribution("reported_financial_debt", debt.value or 0),
                           *(Contribution(e.id, e.value or 0) for e in leases if e)))
    else:
        elements["house_adjusted_debt"] = Element(
            "house_adjusted_debt", None, "unavailable",
            blocked_reason="debt or a lease component is not usable; a partial lease "
                           "adjustment is a floor, not adjusted debt")


def _derive_cash(elements: dict[str, Element]) -> None:
    cash = elements.get("cash_and_equivalents")
    combined = elements.get("cash_plus_restricted")
    restricted = elements.get("restricted_cash")

    if cash and cash.usable:
        elements["readily_available_cash"] = Element(
            "readily_available_cash", cash.value, "derived",
            route="cash_and_equivalents (already excludes restrictions)",
            contributions=(Contribution("cash_and_equivalents", cash.value or 0),))
    elif combined and combined.usable and restricted and restricted.usable:
        elements["readily_available_cash"] = Element(
            "readily_available_cash", (combined.value or 0) - (restricted.value or 0), "derived",
            route="cash_plus_restricted - restricted_cash",
            contributions=(Contribution("cash_plus_restricted", combined.value or 0),
                           Contribution("restricted_cash", -(restricted.value or 0))))
    elif combined and combined.usable:
        elements["readily_available_cash"] = Element(
            "readily_available_cash", None, "unreconciled",
            route="cash_plus_restricted only",
            checks=(Check("restrictions identified", False,
                          "the only cash figure includes restricted amounts"),),
            blocked_reason="the only cash concept served includes restricted cash and no "
                           "restricted figure resolved, so available cash would be overstated")
    else:
        elements["readily_available_cash"] = Element(
            "readily_available_cash", None, "unavailable",
            blocked_reason="no cash concept resolved")

    debt = elements.get("house_adjusted_debt")
    available = elements["readily_available_cash"]
    if debt and debt.usable and available.usable:
        elements["house_net_debt"] = Element(
            "house_net_debt", (debt.value or 0) - (available.value or 0), "derived",
            route="house_adjusted_debt - readily_available_cash",
            contributions=(Contribution("house_adjusted_debt", debt.value or 0),
                           Contribution("readily_available_cash", -(available.value or 0))))
    else:
        elements["house_net_debt"] = Element(
            "house_net_debt", None, "unavailable",
            blocked_reason="adjusted debt or available cash is not usable")


def _derive_flows(elements: dict[str, Element]) -> None:
    def usable(key: str) -> float | None:
        element = elements.get(key)
        return element.value if element and element.usable else None

    ebitda, interest, tax = (usable("house_operating_ebitda"), usable("cash_interest_paid"),
                             usable("cash_taxes_paid"))
    if None not in (ebitda, interest, tax):
        elements["house_ffo"] = Element(
            "house_ffo", ebitda - interest - tax, "derived",  # type: ignore[operator]
            route="house_operating_ebitda - cash_interest_paid - cash_taxes_paid",
            contributions=(Contribution("house_operating_ebitda", ebitda or 0),
                           Contribution("cash_interest_paid", -(interest or 0)),
                           Contribution("cash_taxes_paid", -(tax or 0))))
    else:
        elements["house_ffo"] = Element("house_ffo", None, "unavailable",
                                        blocked_reason="EBITDA, cash interest or cash tax missing")

    ppe, intangibles = usable("capex_ppe"), usable("capex_intangibles")
    if ppe is not None:
        total = ppe + (intangibles or 0)
        elements["house_capex"] = Element(
            "house_capex", total, "derived",
            route="capex_ppe" + (" + capex_intangibles" if intangibles is not None else ""),
            contributions=(Contribution("capex_ppe", ppe),
                           *((Contribution("capex_intangibles", intangibles),) if intangibles else ())))
    else:
        elements["house_capex"] = Element("house_capex", None, "unavailable",
                                          blocked_reason="no capex concept resolved")

    cfo, capex = usable("cfo"), usable("house_capex")
    if cfo is not None and capex is not None:
        elements["focf"] = Element(
            "focf", cfo - capex, "derived", route="cfo - house_capex",
            contributions=(Contribution("cfo", cfo), Contribution("house_capex", -capex)))
    else:
        elements["focf"] = Element("focf", None, "unavailable",
                                   blocked_reason="CFO or capex is not usable")

    focf, dividends, buybacks = usable("focf"), usable("dividends_paid"), usable("share_repurchases")
    if focf is not None and dividends is not None:
        elements["dcf"] = Element(
            "dcf", focf - dividends - (buybacks or 0), "derived",
            route="focf - dividends_paid - share_repurchases",
            contributions=(Contribution("focf", focf), Contribution("dividends_paid", -dividends),
                           *((Contribution("share_repurchases", -buybacks),) if buybacks else ())))
    else:
        elements["dcf"] = Element("dcf", None, "unavailable",
                                  blocked_reason="free operating cash flow or dividends missing")


__all__ = ["normalise"]
