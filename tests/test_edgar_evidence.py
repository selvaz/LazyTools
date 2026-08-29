"""The bundle gathers evidence and names what it could not gather.

Every test here is about the difference between "no data" and a reason. An empty
result is the one shape a caller cannot act on: "this issuer does not exist",
"the period is unreadable", "the concept is a company extension these APIs never
serve" and "the network refused us" all arrive as nothing, and only some of them
are findings about the company.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from lazytools.connectors.edgar.evidence import (
    CONCEPT_CANDIDATES,
    Failure,
    gather,
)

CIK = "0000789019"
FYE = "0630"


def _obs(start: str | None, end: str, val: float, *, accn: str = "a",
         form: str = "10-Q", filed: str = "2026-01-28") -> dict[str, Any]:
    row: dict[str, Any] = {"end": end, "val": val, "accn": accn, "form": form, "filed": filed}
    if start is not None:
        row["start"] = start
    return row


class NotFound(Exception):
    """A 404 shaped like the real client's, so the code under test can tell it
    apart from a fault. ``_is_not_served`` reads ``.response.status_code``."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.response = type("R", (), {"status_code": 404})()


class StubEdgar:
    """A hand-built :class:`EdgarService` whose every answer the test controls.

    Deliberately less forgiving than a convenience fake: it honours ``query``,
    ``form`` and ``limit``, and it distinguishes "no such concept" (a 404) from a
    fault (any other exception). A stub that answered every query identically
    would let the code under test pass where the real client fails.
    """

    def __init__(self, **overrides: Any) -> None:
        self.companies = overrides.get("companies", [
            {"cik": CIK, "ticker": "MSFT", "title": "MICROSOFT CORP"},
        ])
        self.fye: str | None = overrides.get("fye", FYE)
        self.filings: list[dict[str, Any]] = overrides.get("filings", [
            {"accession_no": "0001193125-26-027207", "form": "10-Q",
             "filed_at": "2026-01-28", "report_date": "2025-12-31", "items": [],
             "primary_document": "msft-20251231.htm", "url": "https://example.invalid/q2"},
        ])
        self.concepts: dict[tuple[str, str], Any] = overrides.get("concepts", {})
        self.raises: dict[Any, Exception] = overrides.get("raises", {})
        self.calls: list[tuple[str, Any]] = []

    def resolve_company(self, query: str, *, limit: int = 10) -> list[dict[str, str]]:
        if "resolve_company" in self.raises:
            raise self.raises["resolve_company"]
        self.calls.append(("resolve_company", query))
        q = query.strip().lower()
        hits = [c for c in self.companies
                if c["ticker"].lower() == q or q in c["title"].lower()]
        return hits[:limit]

    def issuer_profile(self, cik: str) -> dict[str, Any]:
        if "issuer_profile" in self.raises:
            raise self.raises["issuer_profile"]
        self.calls.append(("issuer_profile", cik))
        match = next((c for c in self.companies if c["cik"].lstrip("0") == cik.lstrip("0")), None)
        if match is None:
            return {"cik": cik, "name": "", "tickers": [], "fiscal_year_end": None}
        return {"cik": match["cik"], "name": match["title"],
                "tickers": [match["ticker"]], "fiscal_year_end": self.fye}

    def fiscal_year_end(self, cik: str) -> str | None:
        return self.issuer_profile(cik).get("fiscal_year_end")

    def list_filings(self, cik: str, *, form: str | None = None, limit: int = 20,
                     include_history: bool = False) -> list[dict[str, Any]]:
        if "list_filings" in self.raises:
            raise self.raises["list_filings"]
        self.calls.append(("list_filings", (form, limit, include_history)))
        rows = [f for f in self.filings
                if form is None or f["form"].upper() == form.upper()]
        return rows[:limit]

    def company_concept(self, cik: str, taxonomy: str, tag: str) -> dict[str, Any]:
        key = (taxonomy, tag)
        if key in self.raises:
            raise self.raises[key]
        self.calls.append(("company_concept", key))
        units = self.concepts.get(key)
        if units is None:
            raise NotFound(f"{taxonomy}:{tag} not served")
        return {"taxonomy": taxonomy, "tag": tag, "units": {"USD": units}}

    # unused by evidence.gather, present so the stub satisfies the protocol
    def get_filing(self, cik: str, accession_no: str, *, primary_document: str | None = None) -> dict[str, Any]:
        return {}

    def list_filing_documents(self, cik: str, accession_no: str) -> list[dict[str, Any]]:
        return []

    def get_filing_document(self, cik: str, accession_no: str, filename: str) -> dict[str, Any]:
        return {}

    def company_facts(self, cik: str) -> dict[str, Any]:
        return {}


REVENUE = ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax")


def _with_revenue(**overrides: Any) -> StubEdgar:
    return StubEdgar(concepts={REVENUE: [
        _obs("2025-07-01", "2025-12-31", 158946000000),   # H1
        _obs("2025-10-01", "2025-12-31", 81273000000),    # Q2
    ]}, **overrides)


# --- the happy path ------------------------------------------------------- #


def test_the_quarter_is_gathered_with_the_concept_that_answered() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026",
                    metrics=("revenue",), basis="fiscal")
    assert bundle.ok
    metric = bundle.metrics[0]
    assert metric.answered_by == REVENUE
    assert [f.value for f in metric.candidates] == [81273000000]


def test_the_issuer_is_resolved_before_anything_is_fetched() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026", basis="fiscal")
    assert bundle.issuer is not None
    assert (bundle.issuer.cik, bundle.issuer.ticker, bundle.issuer.fiscal_year_end) == (CIK, "MSFT", FYE)


def test_both_readings_are_kept_when_the_caller_does_not_pin_one() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026")
    assert {w.interpretation.basis for w in bundle.windows} == {"fiscal", "calendar"}


def test_the_bundle_records_when_it_was_gathered() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026", basis="fiscal")
    assert bundle.retrieved_at.tzinfo is not None


def test_a_filing_covering_the_period_is_offered_as_a_candidate() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026",
                    metrics=("revenue",), basis="fiscal")
    assert [f.accession for f in bundle.filings] == ["0001193125-26-027207"]


# --- issuer --------------------------------------------------------------- #


def test_an_unknown_company_is_named_as_such() -> None:
    bundle = gather(StubEdgar(), company="Nope Inc", period="Q2 2026")
    assert bundle.failure_codes() == {"issuer_not_found"}
    assert bundle.issuer is None and not bundle.ok


def test_several_matches_without_an_exact_ticker_is_ambiguous_not_a_guess() -> None:
    stub = StubEdgar(companies=[
        {"cik": "0000000001", "ticker": "AAA", "title": "Acme Holdings"},
        {"cik": "0000000002", "ticker": "BBB", "title": "Acme Industries"},
    ])
    bundle = gather(stub, company="Acme", period="Q2 2026")
    assert bundle.failure_codes() == {"issuer_ambiguous"}
    assert len(bundle.failures[0].context["matches"]) == 2


def test_an_exact_ticker_is_not_made_ambiguous_by_other_matches() -> None:
    stub = _with_revenue()
    stub.companies = [
        {"cik": CIK, "ticker": "MSFT", "title": "MICROSOFT CORP"},
        {"cik": "0000000009", "ticker": "MSFX", "title": "Microsoft-ish Ltd"},
    ]
    bundle = gather(stub, company="MSFT", period="Q2 2026", metrics=("revenue",), basis="fiscal")
    assert "issuer_ambiguous" not in bundle.failure_codes()


def test_a_transport_error_while_resolving_is_not_reported_as_a_missing_company() -> None:
    stub = StubEdgar(raises={"resolve_company": ConnectionError("boom")})
    bundle = gather(stub, company="MSFT", period="Q2 2026")
    assert bundle.failure_codes() == {"transport_failure"}


# --- period --------------------------------------------------------------- #


def test_an_unreadable_period_stops_before_any_fetch() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="whenever")
    assert bundle.failure_codes() == {"period_unreadable"}
    assert bundle.filings == []


def test_a_missing_fiscal_year_end_loses_the_fiscal_reading_not_the_request() -> None:
    # The calendar reading needs no year end, so the request survives with a
    # failure explaining what could not be placed.
    stub = _with_revenue(fye=None)
    bundle = gather(stub, company="MSFT", period="Q2 2026")
    assert "fiscal_year_end_unknown" in bundle.failure_codes()
    assert [w.interpretation.basis for w in bundle.windows] == ["calendar"]


def test_a_period_ending_after_its_own_filing_date_is_never_a_candidate() -> None:
    # Not hypothetical: an ESEF index carries a filing whose period ends in 2032.
    # A date that far out cannot match the requested window either, so it is
    # skipped silently -- see the two tests at the end of this file for when the
    # quarantine is reported and when it is not.
    stub = _with_revenue(filings=[
        {"accession_no": "x", "form": "10-Q", "filed_at": "2026-01-28",
         "report_date": "2032-12-31", "items": [], "primary_document": "p.htm", "url": "u"},
    ])
    bundle = gather(stub, company="MSFT", period="Q2 2026", basis="fiscal")
    assert bundle.filings == []
    assert bundle.failure_codes() == {"no_filing_for_period"}


def test_no_filing_covering_the_period_is_said_out_loud() -> None:
    stub = _with_revenue(filings=[
        {"accession_no": "old", "form": "10-K", "filed_at": "2019-07-01",
         "report_date": "2019-06-30", "items": [], "primary_document": "p.htm", "url": "u"},
    ])
    bundle = gather(stub, company="MSFT", period="Q2 2026", basis="fiscal")
    assert "no_filing_for_period" in bundle.failure_codes()


# --- metrics -------------------------------------------------------------- #


def test_an_unknown_metric_is_refused_rather_than_mapped_by_guesswork() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026",
                    metrics=("ebitda",), basis="fiscal")
    assert "metric_unknown" in bundle.failure_codes()
    assert "ebitda" not in CONCEPT_CANDIDATES


def test_an_explicit_concept_bypasses_the_candidate_table() -> None:
    stub = StubEdgar(concepts={("us-gaap", "Weird"): [_obs("2025-10-01", "2025-12-31", 7)]})
    bundle = gather(stub, company="MSFT", period="Q2 2026",
                    concepts=(("us-gaap", "Weird"),), basis="fiscal")
    assert bundle.metrics[0].answered_by == ("us-gaap", "Weird")
    assert [f.value for f in bundle.metrics[0].candidates] == [7]


def test_a_line_absent_from_every_standard_concept_is_a_named_failure() -> None:
    # An extension concept is absent from these APIs, which is a different
    # finding from the company never having reported the line -- and different
    # again from the line existing but not for this period.
    bundle = gather(StubEdgar(), company="MSFT", period="Q2 2026",
                    metrics=("revenue",), basis="fiscal")
    assert bundle.failure_codes() == {"concept_unavailable"}


def test_candidates_that_do_not_apply_are_not_reported_as_failures() -> None:
    # The revenue list spans us-gaap AND ifrs-full on purpose, so a US filer
    # never has three of the four. Reporting each miss would bury real failures
    # under noise meaning only "this list covers both worlds".
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026",
                    metrics=("revenue",), basis="fiscal")
    assert bundle.failures == []
    assert len(CONCEPT_CANDIDATES["revenue"].concepts) > 1


def test_a_period_with_no_matching_fact_is_distinguished_from_a_missing_concept() -> None:
    stub = StubEdgar(concepts={REVENUE: [_obs("2020-01-01", "2020-03-31", 1)]})
    bundle = gather(stub, company="MSFT", period="Q2 2026", metrics=("revenue",), basis="fiscal")
    assert "no_fact_for_period" in bundle.failure_codes()
    assert bundle.metrics[0].answered_by is None


def test_two_concepts_answering_with_different_numbers_is_not_resolved_here() -> None:
    # Total versus continuing operations, or consolidated versus attributable to
    # the parent: both are correct, and they are not the same metric.
    stub = StubEdgar(concepts={
        ("us-gaap", "StockholdersEquity"): [_obs(None, "2025-12-31", 100)],
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"):
            [_obs(None, "2025-12-31", 120)],
    })
    bundle = gather(stub, company="MSFT", period="Q2 2026", metrics=("equity",), basis="fiscal")
    assert "metric_ambiguous" in bundle.failure_codes()
    assert bundle.metrics[0].answered_by is None
    assert sorted(f.value for f in bundle.metrics[0].candidates) == [100.0, 120.0]


def test_two_concepts_agreeing_is_not_an_ambiguity() -> None:
    stub = StubEdgar(concepts={
        ("us-gaap", "StockholdersEquity"): [_obs(None, "2025-12-31", 100)],
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"):
            [_obs(None, "2025-12-31", 100)],
    })
    bundle = gather(stub, company="MSFT", period="Q2 2026", metrics=("equity",), basis="fiscal")
    assert "metric_ambiguous" not in bundle.failure_codes()


def test_the_first_candidate_concept_wins_when_only_it_answers() -> None:
    stub = StubEdgar(concepts={("us-gaap", "Revenues"): [_obs("2025-10-01", "2025-12-31", 5)]})
    bundle = gather(stub, company="MSFT", period="Q2 2026", metrics=("revenue",), basis="fiscal")
    assert bundle.metrics[0].answered_by == ("us-gaap", "Revenues")


def test_an_unreadable_observation_is_surfaced_even_when_others_parse() -> None:
    stub = StubEdgar(concepts={REVENUE: [
        _obs("2025-10-01", "2025-12-31", 81273000000),
        _obs("2025-10-01", "2025-12-31", 99, filed="not-a-date"),
    ]})
    bundle = gather(stub, company="MSFT", period="Q2 2026", metrics=("revenue",), basis="fiscal")
    assert "concept_unreadable" in bundle.failure_codes()


def test_a_wrong_unit_reads_as_no_fact_not_as_a_wrong_number() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026",
                    metrics=("revenue",), currency="EUR", basis="fiscal")
    assert "no_fact_for_period" in bundle.failure_codes()


# --- failure codes -------------------------------------------------------- #


def test_an_unknown_failure_code_cannot_be_constructed() -> None:
    # The codes are a closed set so a caller can branch on them exhaustively.
    with pytest.raises(ValueError, match="unknown failure code"):
        Failure("something_went_wrong", "detail")


def test_ok_is_false_whenever_anything_failed() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026",
                    metrics=("revenue", "ebitda"), basis="fiscal")
    assert not bundle.ok


# --- integration with the shipped fake ------------------------------------ #


def test_the_shipped_fake_client_satisfies_the_gatherer_end_to_end() -> None:
    from lazytools.testing.fake_clients import FakeEdgarClient

    fake = FakeEdgarClient()
    bundle = gather(fake, company="AAPL", period="FY2024", metrics=("revenue",), basis="fiscal")
    # Identity comes from the fake's own canned data, and the window is placed
    # against ITS year end (0928, late September) -- not a December default.
    assert bundle.issuer is not None
    assert (bundle.issuer.ticker, bundle.issuer.fiscal_year_end) == ("AAPL", "0928")
    assert bundle.windows[0].end == date(2024, 9, 28)
    # The fake's 10-K covers period 2024-09-28, so it must surface as a candidate.
    assert "0000320193-24-000123" in {f.accession for f in bundle.filings}


def test_the_shipped_fake_reports_an_absent_concept_rather_than_raising() -> None:
    from lazytools.testing.fake_clients import FakeEdgarClient

    bundle = gather(FakeEdgarClient(), company="AAPL", period="FY2024",
                    concepts=(("us-gaap", "NotAConcept"),), basis="fiscal")
    assert bundle.metrics[0].answered_by is None
    # Nothing was served at all, so this is "the concept is unreachable", not
    # "the concept exists but not for this period" -- a caller acts differently
    # on the two.
    assert "concept_unavailable" in bundle.failure_codes()


def test_windows_are_resolved_against_the_issuers_own_year_end() -> None:
    stub = _with_revenue(fye="1231")
    bundle = gather(stub, company="MSFT", period="Q2 2026", basis="fiscal")
    assert bundle.windows[0].start == date(2026, 4, 1)


# --- units are a property of the metric, not of the caller ---------------- #


def test_a_per_share_metric_is_looked_up_in_the_unit_xbrl_actually_uses() -> None:
    # Measured against the live API: Microsoft's diluted EPS is served under
    # units["USD/shares"]. The SEC's own API documentation says such units are
    # joined by "-per-" ("USD-per-shares"); the payload disagrees, and a unit
    # string that does not exist reads as no data rather than as an error.
    assert CONCEPT_CANDIDATES["eps_diluted"].unit_for("USD") == "USD/shares"
    assert CONCEPT_CANDIDATES["revenue"].unit_for("USD") == "USD"


def test_the_per_share_unit_follows_the_issuers_currency() -> None:
    assert CONCEPT_CANDIDATES["eps_diluted"].unit_for("EUR") == "EUR/shares"


def test_eps_is_found_without_the_caller_naming_its_unit() -> None:
    stub = StubEdgar()
    stub.concepts = {}

    def per_share(cik: str, taxonomy: str, tag: str):
        if (taxonomy, tag) != ("us-gaap", "EarningsPerShareDiluted"):
            raise KeyError(tag)
        return {"taxonomy": taxonomy, "tag": tag, "units": {
            "USD/shares": [_obs("2025-10-01", "2025-12-31", 5.16)]}}

    stub.company_concept = per_share  # type: ignore[method-assign]
    bundle = gather(stub, company="MSFT", period="Q2 2026",
                    metrics=("eps_diluted",), basis="fiscal")
    assert bundle.metrics[0].candidates[0].value == 5.16
    assert bundle.metrics[0].unit == "USD/shares"


# --- a fault is not an absence -------------------------------------------- #


def test_a_timeout_on_one_concept_does_not_produce_a_confident_single_answer() -> None:
    # The dangerous shape: StockholdersEquity answers 100 while the including-NCI
    # concept times out before it could have answered 120. Treating the timeout
    # as "not served" hands back 100 with no failure at all.
    stub = StubEdgar(
        concepts={("us-gaap", "StockholdersEquity"): [_obs(None, "2025-12-31", 100)]},
        raises={("us-gaap",
                 "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"):
                TimeoutError("read timed out")},
    )
    bundle = gather(stub, company="MSFT", period="Q2 2026", metrics=("equity",), basis="fiscal")
    assert "transport_failure" in bundle.failure_codes()
    assert not bundle.ok


def test_a_404_on_a_candidate_that_does_not_apply_stays_silent() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026",
                    metrics=("revenue",), basis="fiscal")
    assert bundle.failures == []


def test_two_concepts_agreeing_at_every_version_is_not_an_ambiguity() -> None:
    # Both report 100 originally and 110 after restatement. Pooling every value
    # gives {100, 110} and would read as a disagreement; per period and per
    # version they never differ.
    rows = [_obs(None, "2025-12-31", 100, accn="a", filed="2026-01-28"),
            _obs(None, "2025-12-31", 110, accn="b", filed="2026-07-28")]
    stub = StubEdgar(concepts={
        ("us-gaap", "StockholdersEquity"): list(rows),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"):
            list(rows),
    })
    bundle = gather(stub, company="MSFT", period="Q2 2026", metrics=("equity",), basis="fiscal")
    assert "metric_ambiguous" not in bundle.failure_codes()


def test_an_8k_with_no_report_date_does_not_match_an_unrelated_period() -> None:
    stub = _with_revenue(filings=[
        {"accession_no": "8k", "form": "8-K", "filed_at": "2026-01-28",
         "report_date": None, "items": ["2.02"], "primary_document": "p.htm", "url": "u"},
    ])
    bundle = gather(stub, company="MSFT", period="FY2019", basis="fiscal")
    assert [f.accession for f in bundle.filings] == []
    assert "no_filing_for_period" in bundle.failure_codes()


def test_an_8k_published_just_after_the_period_is_a_candidate() -> None:
    stub = _with_revenue(filings=[
        {"accession_no": "8k", "form": "8-K", "filed_at": "2026-01-28",
         "report_date": None, "items": ["2.02"], "primary_document": "p.htm", "url": "u"},
    ])
    bundle = gather(stub, company="MSFT", period="Q2 2026", basis="fiscal")
    assert [f.accession for f in bundle.filings] == ["8k"]
    assert bundle.filings[0].reports_results


def test_a_bare_cik_resolves_without_going_through_the_ticker_map() -> None:
    stub = _with_revenue()
    bundle = gather(stub, company=CIK, period="Q2 2026", metrics=("revenue",), basis="fiscal")
    assert bundle.issuer is not None and bundle.issuer.cik == CIK
    assert not any(c[0] == "resolve_company" for c in stub.calls)


def test_a_resolver_entry_missing_its_keys_is_a_failure_not_a_crash() -> None:
    stub = StubEdgar()
    stub.resolve_company = lambda q, *, limit=10: [{"title": "No CIK Corp"}]
    bundle = gather(stub, company="whatever", period="Q2 2026")
    assert "issuer_not_found" in bundle.failure_codes()


def test_as_of_excludes_a_later_restatement_from_the_evidence() -> None:
    stub = StubEdgar(concepts={REVENUE: [
        _obs("2025-10-01", "2025-12-31", 100, accn="a", filed="2026-01-28"),
        _obs("2025-10-01", "2025-12-31", 95, accn="b", filed="2026-07-28"),
    ]})
    later = gather(stub, company="MSFT", period="Q2 2026", metrics=("revenue",), basis="fiscal")
    assert sorted(f.value for f in later.metrics[0].candidates) == [95.0, 100.0]

    frozen = gather(stub, company="MSFT", period="Q2 2026", metrics=("revenue",),
                    basis="fiscal", as_of=date(2026, 6, 30))
    assert [f.value for f in frozen.metrics[0].candidates] == [100.0]


def test_a_misspelled_basis_is_refused_rather_than_filtering_to_nothing() -> None:
    with pytest.raises(ValueError, match="basis must be"):
        gather(_with_revenue(), company="MSFT", period="Q2 2026", basis="fisacl")


def test_a_filings_only_gather_is_ok_when_nothing_failed() -> None:
    bundle = gather(_with_revenue(), company="MSFT", period="Q2 2026", basis="fiscal")
    assert bundle.metrics == [] and bundle.ok


def test_an_unrelated_corrupt_filing_is_skipped_without_being_reported() -> None:
    # SAP's real EDGAR history holds four rows whose report_date precedes their
    # filing date, the oldest from 1998. Reporting each on every question would
    # bury the failures that actually concern the period asked about.
    stub = _with_revenue(filings=[
        {"accession_no": "good", "form": "10-Q", "filed_at": "2026-01-28",
         "report_date": "2025-12-31", "items": [], "primary_document": "p.htm", "url": "u"},
        {"accession_no": "corrupt", "form": "6-K", "filed_at": "1998-10-14",
         "report_date": "1998-10-31", "items": [], "primary_document": "p.htm", "url": "u"},
    ])
    bundle = gather(stub, company="MSFT", period="Q2 2026", basis="fiscal")
    assert [f.accession for f in bundle.filings] == ["good"]
    assert bundle.failures == []


def test_a_corrupt_filing_that_lands_on_the_requested_period_is_reported() -> None:
    # Here the impossible date is the ONLY reason the filing looks relevant, so
    # a reader needs to know why it was set aside.
    stub = _with_revenue(filings=[
        {"accession_no": "corrupt", "form": "10-Q", "filed_at": "2025-06-01",
         "report_date": "2025-12-31", "items": [], "primary_document": "p.htm", "url": "u"},
    ])
    bundle = gather(stub, company="MSFT", period="Q2 2026", basis="fiscal")
    assert "impossible_period" in bundle.failure_codes()
    assert bundle.filings == []
