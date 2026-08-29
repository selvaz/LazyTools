"""Assembling the evidence for a question, before anyone interprets it.

An agent handed the EDGAR tools directly does not reliably use them: a live
trace of this ecosystem's own earnings pipeline recorded **zero** model calls to
the SEC connector on a task about a SEC filer — the model reached for web search
instead. The remedy is not to hard-wire one presumed document, which fails the
long tail (a foreign private issuer files 6-K and 20-F, not 8-K and 10-Q; a
domestic Q4 has no 10-Q at all; Item 2.02 results sometimes sit in the 8-K body
rather than an exhibit). The remedy is to make retrieval deterministic and
*judgment* the only thing left to do.

So this module gathers a **bundle**: who the issuer is, every reading of the
period, which filings could carry it, which facts survived period selection, and
— just as importantly — every way the gathering fell short, as a typed
:class:`Failure` rather than an empty list. What the bundle never contains is an
answer. Choosing among candidate concepts that mean different things, deciding
whether an amendment supersedes, reading a non-GAAP reconciliation: those need
judgment, and they operate over this bundle rather than over the open web.

Nothing here interprets a metric name. ``CONCEPT_CANDIDATES`` is deliberately
small and refuses to guess: a metric it does not know must be named by concept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from lazytools.connectors.edgar.client import EdgarService
from lazytools.connectors.edgar.facts import (
    Fact,
    FactParseError,
    ParseResult,
    parse_concept,
    select,
)
from lazytools.connectors.edgar.period import (
    PeriodParseError,
    ResolvedWindow,
    interpret,
    resolve,
)

#: Forms whose XBRL the SEC's company APIs aggregate, by what they report.
PERIODIC_FORMS: tuple[str, ...] = (
    "10-K", "10-Q", "20-F", "40-F", "6-K",
    "10-K/A", "10-Q/A", "20-F/A", "40-F/A", "6-K/A",
)
#: Forms that carry an earnings announcement rather than a full statement.
#: 6-K appears here AND in :data:`PERIODIC_FORMS` on purpose: a foreign private
#: issuer has no 10-Q, so its interim numbers and its announcement arrive on the
#: same form. Excluding it from the fact forms turned every IFRS interim request
#: into ``no_fact_for_period``.
RESULTS_FORMS: tuple[str, ...] = ("8-K", "6-K", "8-K/A", "6-K/A")
#: The 8-K item code for "Results of Operations and Financial Condition".
RESULTS_ITEM = "2.02"



def _is_not_served(exc: BaseException) -> bool:
    """Is this exception "the API has no such concept", or a genuine fault?

    The distinction decides whether a missing candidate is ignorable. Treating
    every exception as "not served" is how a timed-out request for a second
    concept turns a *possible ambiguity* into a confident single answer: if
    ``StockholdersEquity`` returns 100 and the including-NCI concept times out
    before returning 120, the bundle answers 100 and reports nothing.

    A 404 is the API's own statement that the concept is absent — usually
    because the filer reports that line under its own extension, which these
    APIs never aggregate. Anything else (a timeout, a 429, a 5xx, a dropped
    connection) is a fault, and evidence gathered around it is incomplete.
    ``KeyError``/``LookupError`` is how in-memory fakes say "absent".
    """
    if isinstance(exc, LookupError):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 404


@dataclass(frozen=True)
class MetricSpec:
    """Candidate concepts for one statement line, and the unit it lives in.

    ``unit_kind`` exists because a caller asking for diluted EPS should not have
    to know that XBRL files it as ``USD-per-shares`` rather than ``USD``. Asking
    for the wrong unit reads as no data — the quietest possible way to be wrong —
    so the unit is a property of the metric, derived from the reporting currency
    the caller does supply.
    """

    concepts: tuple[tuple[str, str], ...]
    unit_kind: str = "monetary"

    def unit_for(self, currency: str) -> str:
        """The XBRL unit string for this metric in ``currency``.

        The per-share form is ``"USD/shares"``. The SEC's own API documentation
        says a unit with a numerator and denominator is joined by ``-per-``
        ("USD-per-shares"), but the payload disagrees: Microsoft's diluted EPS
        is served under ``units["USD/shares"]``. Measured against the live API
        on 2026-08-28 and matched to that, because a unit string that does not
        exist reads as no data rather than as an error.
        """
        if self.unit_kind == "per_share":
            return f"{currency}/shares"
        if self.unit_kind == "pure":
            return "pure"
        if self.unit_kind == "shares":
            return "shares"
        return currency


#: Standard concepts worth trying for a handful of unambiguous statement lines.
#:
#: Deliberately short, and deliberately not a normalization table. A filer can
#: report a line under a concept that is *near* the one asked for, or under its
#: own extension, which the SEC's XBRL APIs do not serve at all — substituting a
#: generic standard concept for a custom row is worse than returning nothing,
#: because the number that comes back looks answered. Everything here is a
#: CANDIDATE: :func:`gather` records which concept actually answered, and refuses
#: to choose when two candidates answer with different numbers.
#:
#: Notably absent: EBITDA, adjusted anything, segment lines, and guidance. None
#: of those are XBRL facts on the face of a statement, and pretending otherwise
#: is how a derived number acquires the authority of a reported one.
CONCEPT_CANDIDATES: dict[str, MetricSpec] = {
    "revenue": MetricSpec((
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("ifrs-full", "Revenue"),
    )),
    "operating_income": MetricSpec((
        ("us-gaap", "OperatingIncomeLoss"),
        ("ifrs-full", "ProfitLossFromOperatingActivities"),
    )),
    "net_income": MetricSpec((
        ("us-gaap", "NetIncomeLoss"),
        ("ifrs-full", "ProfitLoss"),
    )),
    "eps_basic": MetricSpec((
        ("us-gaap", "EarningsPerShareBasic"),
        ("ifrs-full", "BasicEarningsLossPerShare"),
    ), unit_kind="per_share"),
    "eps_diluted": MetricSpec((
        ("us-gaap", "EarningsPerShareDiluted"),
        ("ifrs-full", "DilutedEarningsLossPerShare"),
    ), unit_kind="per_share"),
    "operating_cash_flow": MetricSpec((
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    )),
    "capex": MetricSpec((
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
    )),
    "assets": MetricSpec((("us-gaap", "Assets"), ("ifrs-full", "Assets"))),
    "liabilities": MetricSpec((("us-gaap", "Liabilities"), ("ifrs-full", "Liabilities"))),
    "equity": MetricSpec((
        ("us-gaap", "StockholdersEquity"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        ("ifrs-full", "Equity"),
    )),
}


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #
#: Every way gathering can fall short, named. A bare empty result cannot be acted
#: on: "this issuer does not exist", "the period is unreadable", "the concept is
#: an extension we cannot reach" and "the network refused us" all arrive as no
#: data, and only some of them are findings about the company.
FAILURE_CODES = frozenset({
    "issuer_not_found",
    "issuer_ambiguous",
    "period_unreadable",
    "fiscal_year_end_unknown",
    "impossible_period",
    "concept_unavailable",
    "concept_unreadable",
    "no_fact_for_period",
    "metric_ambiguous",
    "metric_unknown",
    "no_filing_for_period",
    "transport_failure",
})


@dataclass(frozen=True)
class Failure:
    """One named way the gathering fell short, with what it was reaching for."""

    code: str
    detail: str
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in FAILURE_CODES:
            raise ValueError(f"unknown failure code {self.code!r}; expected one of {sorted(FAILURE_CODES)}")


# --------------------------------------------------------------------------- #
# Bundle pieces
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IssuerIdentity:
    """Who the numbers belong to, resolved before anything is fetched."""

    cik: str
    name: str
    ticker: str | None
    fiscal_year_end: str | None


@dataclass(frozen=True)
class FilingRef:
    """One filing that could carry the requested period."""

    accession: str
    form: str
    filed: date
    report_date: date | None
    items: tuple[str, ...]
    primary_document: str
    url: str

    @property
    def reports_results(self) -> bool:
        """An 8-K/6-K furnishing results of operations (Item 2.02).

        Only a hint about where to look. The release itself is usually an
        exhibit, the item can also be furnished alongside 7.01, and one filing
        may carry several EX-99 documents — which of them is the release is a
        judgement, not a lookup.
        """
        return RESULTS_ITEM in self.items


@dataclass(frozen=True)
class MetricEvidence:
    """What was found for one requested metric, and under which concept.

    ``candidates`` holds every fact that survived period selection, across every
    concept tried. ``answered_by`` names the concept that produced them — ``None``
    when nothing did, or when more than one concept produced *different* numbers
    and choosing between them is not this module's call.
    """

    metric: str
    answered_by: tuple[str, str] | None
    candidates: list[Fact]
    tried: tuple[tuple[str, str], ...]
    #: The XBRL unit actually queried. Recorded because it is derived from the
    #: metric (EPS lives in ``USD-per-shares``, not ``USD``) and a reader
    #: checking a number needs to know which unit produced it.
    unit: str = ""


@dataclass(frozen=True)
class EvidenceBundle:
    """Everything gathered for one question. Contains no answer, by design."""

    request: str
    issuer: IssuerIdentity | None
    windows: list[ResolvedWindow]
    filings: list[FilingRef]
    metrics: list[MetricEvidence]
    failures: list[Failure]
    retrieved_at: datetime

    @property
    def ok(self) -> bool:
        """Nothing failed, and every metric that was asked for was answered.

        A call that asked for no metrics (wanting only the candidate filings) is
        ``ok`` when nothing failed — requiring an answered metric would make a
        successful filings-only gather permanently report failure.
        """
        return not self.failures and all(m.answered_by for m in self.metrics)

    def failure_codes(self) -> set[str]:
        """The distinct codes present, for a caller branching on outcome."""
        return {f.code for f in self.failures}


# --------------------------------------------------------------------------- #
# Gathering
# --------------------------------------------------------------------------- #
def gather(
    client: EdgarService,
    *,
    company: str,
    period: str,
    metrics: tuple[str, ...] = (),
    concepts: tuple[tuple[str, str], ...] = (),
    currency: str = "USD",
    forms: tuple[str, ...] = PERIODIC_FORMS,
    basis: str | None = None,
    as_of: date | None = None,
) -> EvidenceBundle:
    """Assemble the evidence for one question against one issuer.

    Args:
        client: any :class:`~lazytools.connectors.edgar.client.EdgarService`.
        company: a ticker or company name, resolved through EDGAR's own map.
        period: a phrase like ``"Q2 2026"``. Read into every plausible window;
            see :mod:`~lazytools.connectors.edgar.period` for why that is a list.
        metrics: names from :data:`CONCEPT_CANDIDATES`. An unknown name is a
            ``metric_unknown`` failure, never a guess.
        concepts: extra ``(taxonomy, tag)`` pairs to try verbatim, for anything
            the candidate table deliberately does not cover.
        currency: the issuer's reporting currency. ``"USD"`` suits US filers; a
            euro-reporting issuer needs ``"EUR"``, and passing the wrong one reads
            as no data. The XBRL unit is derived from it per metric, so diluted
            EPS is looked up as ``USD/shares`` without the caller saying so.
        forms: which forms' facts to accept.
        basis: ``"fiscal"`` or ``"calendar"`` to pin the reading when the caller
            already knows; ``None`` keeps both, which is the honest default.
        as_of: ignore anything filed after this date. Without it the same request
            answers differently once a later filing lands, so a bundle cannot be
            reconstructed from the question that produced it; with it, gathering
            is reproducible. Applies to candidate filings AND to facts.

    Never raises for a missing issuer, period or fact — those are :class:`Failure`
    entries on the returned bundle, because a caller needs to tell them apart.
    """
    failures: list[Failure] = []
    retrieved_at = datetime.now(timezone.utc)

    issuer = _resolve_issuer(client, company, failures)
    if issuer is None:
        return EvidenceBundle(request=f"{company} {period}", issuer=None, windows=[],
                              filings=[], metrics=[], failures=failures, retrieved_at=retrieved_at)

    windows = _resolve_windows(period, issuer, basis, failures)
    if not windows:
        return EvidenceBundle(request=f"{company} {period}", issuer=issuer, windows=[],
                              filings=[], metrics=[], failures=failures, retrieved_at=retrieved_at)

    filings = _candidate_filings(client, issuer, windows, failures, as_of=as_of)
    wanted = _concepts_for(metrics, concepts, failures)
    evidence = [
        _gather_metric(client, issuer, name, spec, windows, currency, forms, failures,
                       as_of=as_of)
        for name, spec in wanted
    ]

    return EvidenceBundle(
        request=f"{company} {period}",
        issuer=issuer,
        windows=windows,
        filings=filings,
        metrics=evidence,
        failures=failures,
        retrieved_at=retrieved_at,
    )


def _resolve_issuer(client: EdgarService, company: str, failures: list[Failure]) -> IssuerIdentity | None:
    """Name, ticker or CIK to one identity, or a named reason for stopping."""
    query = company.strip()
    if _looks_like_cik(query):
        # A CIK is the least ambiguous identifier EDGAR has, and resolve_company
        # matches only tickers and titles -- so a bare CIK finds nothing there.
        return _profile(client, query, failures)

    try:
        matches = client.resolve_company(query, limit=5)
    except Exception as exc:  # transport, or a malformed query the client refuses
        failures.append(Failure("transport_failure", f"resolving {company!r} failed: {exc}",
                                {"company": company}))
        return None
    if not matches:
        failures.append(Failure("issuer_not_found", f"{company!r} matched no EDGAR filer",
                                {"company": company}))
        return None
    # More than one match is only ambiguous when the query did not name one
    # exactly. EDGAR's own ordering puts an exact ticker hit first, and treating
    # "AAPL" as ambiguous because other titles contain it would refuse the
    # clearest possible query. `.get` throughout: this is a protocol another
    # implementation may satisfy, and a KeyError here would break the contract
    # that gathering reports its failures rather than raising them.
    top = matches[0]
    if len(matches) > 1 and str(top.get("ticker") or "").lower() != query.lower():
        failures.append(Failure(
            "issuer_ambiguous",
            f"{company!r} matched {len(matches)} filers; name the ticker or the CIK",
            {"company": company, "matches": [
                {"cik": m.get("cik"), "ticker": m.get("ticker"), "title": m.get("title")}
                for m in matches]},
        ))
        return None
    cik = str(top.get("cik") or "")
    if not cik:
        failures.append(Failure("issuer_not_found",
                                f"{company!r} resolved to an entry carrying no CIK",
                                {"company": company, "match": dict(top)}))
        return None
    profile = _profile(client, cik, failures)
    if profile is None:
        return None
    # The resolver's title and ticker are the ones the caller's query matched;
    # prefer them over the submissions name so provenance shows what was asked
    # for, falling back when the resolver left them blank.
    return IssuerIdentity(
        cik=profile.cik,
        name=str(top.get("title") or profile.name),
        ticker=str(top.get("ticker") or "") or profile.ticker,
        fiscal_year_end=profile.fiscal_year_end,
    )


def _looks_like_cik(query: str) -> bool:
    raw = query.upper().removeprefix("CIK").strip().lstrip("-").strip()
    return raw.isdigit() and 1 <= len(raw) <= 10


def _profile(client: EdgarService, cik: str, failures: list[Failure]) -> IssuerIdentity | None:
    try:
        data = client.issuer_profile(cik)
    except Exception as exc:
        failures.append(Failure("transport_failure", f"reading the issuer profile failed: {exc}",
                                {"cik": cik}))
        return None
    resolved = str(data.get("cik") or cik)
    name = str(data.get("name") or "")
    if not name:
        failures.append(Failure("issuer_not_found",
                                f"CIK {resolved} has no registrant on file",
                                {"cik": resolved}))
        return None
    tickers = [str(t) for t in (data.get("tickers") or []) if t]
    return IssuerIdentity(cik=resolved, name=name, ticker=tickers[0] if tickers else None,
                          fiscal_year_end=data.get("fiscal_year_end"))


def _resolve_windows(
    period: str, issuer: IssuerIdentity, basis: str | None, failures: list[Failure]
) -> list[ResolvedWindow]:
    try:
        readings = interpret(period)
    except PeriodParseError as exc:
        failures.append(Failure("period_unreadable", str(exc), {"period": period}))
        return []
    if basis is not None:
        if basis not in ("fiscal", "calendar"):
            # Filtering on a typo would leave zero readings and an empty bundle
            # with nothing anywhere explaining why.
            raise ValueError(f"basis must be 'fiscal', 'calendar' or None, got {basis!r}")
        readings = [r for r in readings if r.basis == basis]

    windows: list[ResolvedWindow] = []
    for reading in readings:
        try:
            windows.append(resolve(reading, fiscal_year_end=issuer.fiscal_year_end))
        except ValueError as exc:
            # A fiscal reading with no usable year end. The calendar reading may
            # still resolve, so this is recorded and the loop continues rather
            # than abandoning the whole request.
            failures.append(Failure(
                "fiscal_year_end_unknown",
                f"cannot place {reading.label}: {exc}",
                {"cik": issuer.cik, "fiscal_year_end": issuer.fiscal_year_end},
            ))
    return windows


#: How long after a period ends an announcement about it may still arrive.
#: A results 8-K/6-K carries NO report_date, so it cannot be matched on the
#: period it covers -- only on when it was published. Without a bound, such a
#: filing matches every period ever requested, and an unrelated recent 8-K would
#: satisfy a question about 2019 while suppressing ``no_filing_for_period``.
ANNOUNCEMENT_WINDOW_DAYS = 120


def _candidate_filings(
    client: EdgarService, issuer: IssuerIdentity, windows: list[ResolvedWindow],
    failures: list[Failure], *, as_of: date | None = None,
) -> list[FilingRef]:
    """Filings that could carry the requested period.

    Includes results announcements (8-K/6-K) as well as the periodic forms: the
    numbers may be in the 10-Q while the narrative a reader wants is in the
    release, and a bundle offering only one of them would quietly decide which
    question was being asked.

    The recent block is read first, in one request. History is reached for only
    when nothing matched AND the requested period predates everything the recent
    block held -- the ordinary question costs one request, and the archive is
    walked only when the answer provably cannot be anywhere else.
    """
    rows = _list_filings(client, issuer, failures)
    if rows is None:
        return []
    refs = _filings_in_window(rows, windows, failures, as_of)

    oldest = min((r for r in (_date(x.get("filed_at")) for x in rows) if r), default=None)
    if not refs and oldest is not None and any(w.end < oldest for w in windows):
        for form in (*PERIODIC_FORMS, *RESULTS_FORMS):
            extra = _list_filings(client, issuer, failures, form=form, limit=25,
                                  include_history=True)
            if extra:
                rows.extend(extra)
        refs = _filings_in_window(rows, windows, failures, as_of)

    if not refs:
        failures.append(Failure(
            "no_filing_for_period",
            "no filing covers any reading of the requested period",
            {"cik": issuer.cik,
             "windows": [f"{w.start.isoformat()}..{w.end.isoformat()}" for w in windows]},
        ))
    return refs


def _list_filings(
    client: EdgarService, issuer: IssuerIdentity, failures: list[Failure], *,
    form: str | None = None, limit: int = 1000, include_history: bool = False,
) -> list[dict[str, Any]] | None:
    """One filings request, with a transport failure recorded rather than raised."""
    try:
        return list(client.list_filings(issuer.cik, form=form, limit=limit,
                                        include_history=include_history))
    except Exception as exc:
        failures.append(Failure("transport_failure", f"listing filings failed: {exc}",
                                {"cik": issuer.cik, "form": form}))
        return None


def _filings_in_window(
    rows: list[dict[str, Any]], windows: list[ResolvedWindow], failures: list[Failure],
    as_of: date | None,
) -> list[FilingRef]:
    accepted: dict[str, FilingRef] = {}
    known_forms = {f.upper() for f in (*PERIODIC_FORMS, *RESULTS_FORMS)}
    for row in rows:
        filed = _date(row.get("filed_at"))
        if filed is None:
            continue
        if as_of is not None and filed > as_of:
            continue
        form = str(row.get("form", ""))
        if form.upper() not in known_forms:
            continue
        reported = _date(row.get("report_date"))
        if reported is not None:
            in_window = any(abs((reported - w.end).days) <= w.tolerance_days for w in windows)
            if reported > filed:
                # A covered period ending after the filing that reports it cannot
                # have happened. Real and not rare: SAP's own EDGAR history holds
                # four such rows out of 666 (one filed 2004-05-10 claiming to
                # cover 2004-10-05 -- a transposed day and month). Always skipped;
                # only REPORTED when the bad date would otherwise have made this
                # filing a candidate. An unrelated corrupt row from 1998 is not a
                # finding about the period someone asked for, and reporting it
                # would bury the failures that are.
                if in_window:
                    failures.append(Failure(
                        "impossible_period",
                        f"filing {row.get('accession_no')} reports a period ending "
                        f"{reported.isoformat()} but was filed {filed.isoformat()}; "
                        "it matches the requested period only by that impossible date",
                        {"accession": row.get("accession_no"), "form": form},
                    ))
                continue
            if not in_window:
                continue
        elif not any(
            0 <= (filed - w.end).days <= ANNOUNCEMENT_WINDOW_DAYS for w in windows
        ):
            # No report_date: matched on publication instead, within the window
            # an announcement about that period could plausibly appear.
            continue
        accession = str(row.get("accession_no", ""))
        accepted.setdefault(accession, FilingRef(
            accession=accession,
            form=form,
            filed=filed,
            report_date=reported,
            items=tuple(row.get("items") or ()),
            primary_document=str(row.get("primary_document", "")),
            url=str(row.get("url", "")),
        ))
    return list(accepted.values())



def _concepts_for(
    metrics: tuple[str, ...], concepts: tuple[tuple[str, str], ...], failures: list[Failure],
) -> list[tuple[str, MetricSpec]]:
    wanted: list[tuple[str, MetricSpec]] = []
    for name in metrics:
        tries = CONCEPT_CANDIDATES.get(name)
        if tries is None:
            failures.append(Failure(
                "metric_unknown",
                f"{name!r} has no candidate concepts here; pass concepts=((taxonomy, tag), ...) "
                "explicitly rather than having one guessed",
                {"metric": name, "known": sorted(CONCEPT_CANDIDATES)},
            ))
            continue
        wanted.append((name, tries))
    for entry in concepts:
        # A 2-tuple takes the caller's currency; a 3-tuple names the unit kind,
        # because a caller reaching past the candidate table may well be after a
        # share count or a ratio, and querying those as USD reads as no data.
        taxonomy, tag = entry[0], entry[1]
        kind = entry[2] if len(entry) > 2 else "monetary"
        wanted.append((f"{taxonomy}:{tag}", MetricSpec(((taxonomy, tag),), unit_kind=kind)))
    return wanted


def _gather_metric(
    client: EdgarService, issuer: IssuerIdentity, metric: str,
    spec: MetricSpec, windows: list[ResolvedWindow],
    currency: str, forms: tuple[str, ...], failures: list[Failure],
    *, as_of: date | None = None,
) -> MetricEvidence:
    """Try each candidate concept; refuse to choose when two disagree.

    A candidate the API does not serve is **not** reported while another
    candidate answers. The candidate lists deliberately span taxonomies, so a US
    filer will never have the ``ifrs-full`` entry and an IFRS filer will never
    have the ``us-gaap`` one: reporting each miss would bury the real failures
    under noise that means nothing more than "this list covers both worlds".
    It only becomes a finding when *nothing* answered — and then it is the
    interesting one, because a line absent from every standard concept is
    usually a line the filer reports under its own extension.
    """
    tries = spec.concepts
    unit = spec.unit_for(currency)
    answers: list[tuple[tuple[str, str], list[Fact]]] = []
    unavailable: list[str] = []
    served = 0
    for taxonomy, tag in tries:
        try:
            payload = client.company_concept(issuer.cik, taxonomy, tag)
        except Exception as exc:
            if not _is_not_served(exc):
                # A fault, not an absence. Reported immediately so a caller can
                # see the evidence is incomplete even if another candidate goes
                # on to answer -- an unanswered candidate might have disagreed.
                failures.append(Failure(
                    "transport_failure",
                    f"fetching {taxonomy}:{tag} failed: {exc}; the evidence for "
                    f"{metric} is incomplete",
                    {"cik": issuer.cik, "taxonomy": taxonomy, "tag": tag, "metric": metric},
                ))
                continue
            unavailable.append(f"{taxonomy}:{tag} ({exc})")
            continue
        if not payload:
            unavailable.append(f"{taxonomy}:{tag} (empty payload)")
            continue
        served += 1
        try:
            parsed: ParseResult = parse_concept(payload)
        except FactParseError as exc:  # pragma: no cover - parse_concept does not raise
            failures.append(Failure("concept_unreadable", str(exc),
                                    {"taxonomy": taxonomy, "tag": tag}))
            continue
        if parsed.dropped:
            # Reported even when this concept goes on to answer: the row that
            # failed to parse may be the one carrying a restatement, in which
            # case the surviving original is returned as if it were current.
            failures.append(Failure(
                "concept_unreadable",
                f"{len(parsed.dropped)} of {parsed.seen} observations for {taxonomy}:{tag} "
                "could not be read; a dropped row may be the one carrying a restatement",
                {"taxonomy": taxonomy, "tag": tag, "dropped": len(parsed.dropped)},
            ))
        usable = parsed.facts if as_of is None else [f for f in parsed.facts if f.filed <= as_of]
        hits = [f for w in windows for f in select(usable, w, unit=unit, forms=forms)]
        if hits:
            answers.append(((taxonomy, tag), _dedupe(hits)))

    if not answers:
        if served == 0:
            failures.append(Failure(
                "concept_unavailable",
                f"no standard concept for {metric} is served for this filer; the line "
                "may be reported under a company extension, which these APIs do not aggregate",
                {"cik": issuer.cik, "metric": metric, "tried": unavailable},
            ))
        else:
            failures.append(Failure(
                "no_fact_for_period",
                f"no {metric} fact covers the requested period in {unit}",
                {"metric": metric, "tried": [f"{t}:{g}" for t, g in tries], "unit": unit},
            ))
        return MetricEvidence(metric=metric, answered_by=None, candidates=[], tried=tries, unit=unit)

    if len(answers) > 1 and _concepts_disagree(answers):
        # Two standard concepts both answering, with different numbers for the
        # SAME period at the SAME version, is a statement about the filing --
        # total versus continuing operations, or consolidated versus
        # attributable to the parent. Picking the first would turn that into a
        # silent choice between different metrics.
        failures.append(Failure(
            "metric_ambiguous",
            f"{len(answers)} concepts answered for {metric} with different values for "
            "the same period; they are not synonyms and choosing is a judgement",
            {"metric": metric,
             "answers": [{"concept": f"{t}:{g}", "values": sorted({f.value for f in facts})}
                         for (t, g), facts in answers]},
        ))
        return MetricEvidence(metric=metric, answered_by=None,
                              candidates=[f for _, facts in answers for f in facts],
                              tried=tries, unit=unit)

    concept, facts = answers[0]
    return MetricEvidence(metric=metric, answered_by=concept, candidates=facts,
                          tried=tries, unit=unit)


def _concepts_disagree(answers: list[tuple[tuple[str, str], list[Fact]]]) -> bool:
    """Do two concepts report different numbers for the same period and version?

    Pooling every value and asking whether more than one distinct number appears
    is wrong twice over. Two concepts that agree at every version still pool to
    {original, restated} and would read as a disagreement; and two concepts that
    genuinely differ in one period could be masked by a third that matches both.

    So the comparison is per ``(start, end)``, and within each period only the
    latest-filed value of each concept is compared -- like with like.
    """
    by_period: dict[tuple[Any, Any], dict[tuple[str, str], Fact]] = {}
    for concept, facts in answers:
        for fact in facts:
            latest = by_period.setdefault((fact.start, fact.end), {})
            held = latest.get(concept)
            if held is None or fact.filed > held.filed:
                latest[concept] = fact
    return any(
        len({f.value for f in per_concept.values()}) > 1
        for per_concept in by_period.values()
    )


def _dedupe(facts: list[Fact]) -> list[Fact]:
    """Drop repeats from overlapping windows, preserving order."""
    seen: set[tuple[Any, ...]] = set()
    out: list[Fact] = []
    for f in facts:
        key = (f.concept, f.unit, f.start, f.end, f.accession, f.value)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


__all__ = [
    "CONCEPT_CANDIDATES",
    "FAILURE_CODES",
    "PERIODIC_FORMS",
    "RESULTS_FORMS",
    "RESULTS_ITEM",
    "EvidenceBundle",
    "Failure",
    "FilingRef",
    "IssuerIdentity",
    "MetricEvidence",
    "MetricSpec",
    "gather",
]
