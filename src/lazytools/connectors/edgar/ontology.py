"""Deciding what kind of issuer this is, before fetching a single figure.

An analysis that reaches for leverage and coverage has already assumed the
issuer is an ordinary non-financial corporate in going concern. For a bank an
insurer, a project company or a captive-finance industrial, those metrics are
not weak — they are meaningless, and computing them produces numbers that look
fine. So the classification has to happen first.

Two signals, because neither is sufficient.

**The SIC code** is free (it is in the submissions JSON already fetched for the
fiscal year end) and settles the obvious cases: a bank files under 6022, an
insurer under 6311, a REIT under 6798, a utility under 4911.

**The filing's own report index** catches what the SIC cannot. Deere files under
3523, Farm Machinery & Equipment, and runs a finance business with its own
funding — nothing in the code says so, and a run against it collapsed after
twenty wasted requests. But its ``FilingSummary.xml`` names reports about
financial services and financing receivables, and that index costs one request
and is the filing's own table of contents.

What this module does **not** do is switch ontology on a keyword. Cisco's index
mentions financing receivables too, and Cisco is not Deere: the signal says an
overlay is needed to decide materiality, not that corporate metrics are wrong.
A signal raises a question; it does not answer it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from lazytools.connectors.edgar.client import EdgarService
from lazytools.connectors.edgar.statements import list_reports

Ontology = Literal["corporate", "bank", "insurer", "reit", "utility", "unclassified"]

#: SIC ranges that settle the ontology on their own. Ranges rather than codes
#: because the SEC's own office groupings work that way, and a code list would
#: silently misclassify every issuer filing under one it omits.
_SIC_RANGES: tuple[tuple[int, int, Ontology], ...] = (
    (6020, 6199, "bank"),      # depository and non-depository credit
    (6300, 6411, "insurer"),   # insurance carriers, agents and brokers
    (6798, 6798, "reit"),      # real estate investment trusts
    (4900, 4949, "utility"),   # electric, gas, water and combination
)

#: Structural signals read from the filing's own report index. Each one means
#: "the corporate vocabulary may not apply here", never "it does not".
_SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("captive_finance",
     r"financial services|financ\w* receivable|captive finance",
     "a financing business inside an industrial group funds itself separately, so "
     "consolidated debt, interest and operating cash flow mix two businesses"),
    ("non_recourse",
     r"non-?recourse",
     "debt without recourse to the parent cannot be removed from leverage unless the "
     "earnings and cash that service it are removed on the same perimeter"),
    ("consolidated_vie",
     r"variable interest entit",
     "consolidated entities the issuer does not wholly own can carry debt and cash "
     "that are not freely available to it"),
    ("rate_regulated",
     r"regulatory asset|rate.?regulated|regulated operations|allowed (?:rate of )?return",
     "a regulator sets the returns and the recovery of costs, which dominates business "
     "risk and changes what the cash-flow ratios mean"),
    ("insurance_operations",
     r"unpaid loss|loss reserve|policyholder|reinsurance recoverable",
     "policyholder obligations and reserve adequacy replace leverage as the solvency "
     "question"),
    ("banking_operations",
     r"allowance for credit loss|loans and leases|deposit liabilit",
     "capital adequacy and asset quality replace leverage as the solvency question"),
)


@dataclass(frozen=True)
class Signal:
    """One structural finding, with the report names that raised it."""

    name: str
    why: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    """What kind of issuer this is, and what must be settled before analysis."""

    cik: str
    name: str
    sic: str | None
    sic_description: str | None
    ontology: Ontology
    signals: tuple[Signal, ...]
    detail: str

    @property
    def corporate_metrics_apply(self) -> bool:
        """May leverage and coverage be computed without an overlay first?

        False whenever the SIC settles a different ontology, or the filing's own
        index raises a structural signal. False does not mean the issuer is
        exotic — for Cisco it means "decide whether the finance business is
        material before treating consolidated figures as industrial ones".
        """
        return self.ontology == "corporate" and not self.signals

    @property
    def signal_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.signals)


def classify(
    client: EdgarService, cik: str, accession: str | None = None
) -> Classification:
    """Classify an issuer from its SIC and, when given, its filing's index.

    Args:
        client: any :class:`EdgarService`.
        cik: the issuer's CIK.
        accession: a filing whose report index should be scanned. Omitting it
            gives the SIC-only classification, which is cheap and incomplete —
            it is exactly the classification that misses Deere.

    Never raises for a filing whose index cannot be read: that becomes a
    ``no_index`` note, because an unscanned filing is not a filing with no
    structural signals.
    """
    profile = client.issuer_profile(cik)
    sic = profile.get("sic")
    ontology = _ontology_from_sic(sic)

    signals: tuple[Signal, ...] = ()
    scanned = False
    if accession:
        try:
            names = [report.short_name for report in list_reports(client, cik, accession)]
            signals = _signals_from(names)
            scanned = True
        except Exception:  # noqa: BLE001 - an unreadable index is a note, not a failure
            signals = ()

    return Classification(
        cik=str(profile.get("cik") or cik),
        name=str(profile.get("name") or ""),
        sic=sic,
        sic_description=profile.get("sic_description"),
        ontology=ontology,
        signals=signals,
        detail=_detail(ontology, sic, profile.get("sic_description"), signals, scanned),
    )


def _ontology_from_sic(sic: str | None) -> Ontology:
    if not sic or not str(sic).strip().isdigit():
        return "unclassified"
    code = int(str(sic).strip())
    for low, high, ontology in _SIC_RANGES:
        if low <= code <= high:
            return ontology
    return "corporate"


def _signals_from(report_names: list[str]) -> tuple[Signal, ...]:
    found: list[Signal] = []
    for name, pattern, why in _SIGNALS:
        matches = tuple(n for n in report_names if re.search(pattern, n, re.I))
        if matches:
            found.append(Signal(name=name, why=why, evidence=matches[:4]))
    return tuple(found)


def _detail(
    ontology: Ontology, sic: str | None, sic_description: str | None,
    signals: tuple[Signal, ...], scanned: bool,
) -> str:
    where = f"SIC {sic} ({sic_description})" if sic else "no SIC on file"
    if ontology != "corporate":
        article = "an" if ontology[0] in "aeiou" else "a"
        return (f"{where} settles this as {article} {ontology}: the corporate leverage and "
                "coverage vocabulary does not apply and a different ontology must be loaded")
    if not scanned:
        return (f"{where} indicates an ordinary corporate, but no filing index was scanned "
                "— the SIC alone misses a captive finance business, which is how Deere "
                "reads as farm machinery")
    if not signals:
        return f"{where}, and the filing's index raised no structural signal"
    return (f"{where}, but the filing's index raises {len(signals)} structural signal(s) "
            f"({', '.join(s.name for s in signals)}) that must be settled before "
            "consolidated figures are treated as ordinary corporate ones")


__all__ = ["Classification", "Ontology", "Signal", "classify"]
