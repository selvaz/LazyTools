"""Classifying an issuer before any figure is fetched.

The case that forced this module: Deere files under SIC 3523, Farm Machinery &
Equipment, and runs a finance business with its own funding. A run that trusted
the SIC collapsed after twenty wasted requests when every debt component came
back unserved. Its filing's own report index names financial-services reports,
and that index costs one request.
"""

from __future__ import annotations

from lazytools.connectors.edgar.ontology import classify


class _Stub:
    """An EdgarService whose SIC and report index the test sets."""

    def __init__(self, sic: str | None, reports: list[str] | None = None,
                 index_fails: bool = False) -> None:
        self.sic, self.reports, self.index_fails = sic, reports or [], index_fails

    def issuer_profile(self, cik: str) -> dict:
        return {"cik": cik, "name": "Test Co", "tickers": ["TST"],
                "fiscal_year_end": "1231", "sic": self.sic,
                "sic_description": "Some Industry"}

    def get_filing_document(self, cik, accession_no, filename, *, raw=False) -> dict:
        if self.index_fails:
            raise RuntimeError("no summary")
        blocks = "".join(
            f"<Report><HtmlFileName>R{i}.htm</HtmlFileName><ShortName>{n}</ShortName>"
            f"<MenuCategory>Notes</MenuCategory></Report>"
            for i, n in enumerate(self.reports, start=1))
        return {"content": f"<FilingSummary>{blocks}</FilingSummary>"}


# --- the SIC settles the obvious cases ------------------------------------- #


def test_a_bank_is_settled_by_its_sic_alone() -> None:
    result = classify(_Stub("6021"), "0000019617")
    assert result.ontology == "bank" and not result.corporate_metrics_apply


def test_an_insurer_a_reit_and_a_utility_are_settled_the_same_way() -> None:
    assert classify(_Stub("6311"), "x").ontology == "insurer"
    assert classify(_Stub("6798"), "x").ontology == "reit"
    assert classify(_Stub("4911"), "x").ontology == "utility"


def test_an_ordinary_industrial_sic_reads_as_corporate() -> None:
    assert classify(_Stub("2840"), "x").ontology == "corporate"


def test_a_missing_sic_is_unclassified_rather_than_assumed_corporate() -> None:
    result = classify(_Stub(None), "x")
    assert result.ontology == "unclassified" and not result.corporate_metrics_apply


# --- the index catches what the SIC cannot --------------------------------- #


def test_deeres_finance_business_is_found_despite_a_farm_machinery_sic() -> None:
    result = classify(
        _Stub("3523", ["Financial Services Segment", "Financing Receivables - Details"]),
        "0000315189", accession="x",
    )
    assert result.ontology == "corporate"
    assert "captive_finance" in result.signal_names
    assert not result.corporate_metrics_apply


def test_the_signal_carries_the_report_names_that_raised_it() -> None:
    result = classify(_Stub("3523", ["Financial Services Segment"]), "x", accession="x")
    assert result.signals[0].evidence == ("Financial Services Segment",)
    assert "funds itself separately" in result.signals[0].why


def test_non_recourse_debt_is_a_signal_on_its_own() -> None:
    result = classify(_Stub("4911", ["Non-Recourse Long-Term Debt"]), "x", accession="x")
    assert "non_recourse" in result.signal_names


def test_a_clean_index_leaves_an_ordinary_corporate_clear_to_proceed() -> None:
    result = classify(_Stub("2840", ["Consolidated Balance Sheets", "Segments"]),
                      "x", accession="x")
    assert result.corporate_metrics_apply
    assert "raised no structural signal" in result.detail


# --- an unscanned filing is not a clean one -------------------------------- #


def test_without_an_accession_the_classification_says_it_is_incomplete() -> None:
    result = classify(_Stub("3523"), "x")
    assert result.signals == ()
    assert "no filing index was scanned" in result.detail
    # The SIC-only answer is exactly the one that misses Deere, so it must not
    # read as a clean bill of health.
    assert result.corporate_metrics_apply is True
    assert "misses a captive finance business" in result.detail


def test_an_unreadable_index_does_not_raise_out_of_the_gate() -> None:
    result = classify(_Stub("2840", index_fails=True), "x", accession="x")
    assert result.signals == ()


# --- a signal asks a question, it does not answer it ----------------------- #


def test_a_signal_does_not_change_the_ontology() -> None:
    # Cisco's index mentions financing receivables and Cisco is not Deere. The
    # signal means "settle materiality first", not "these metrics are wrong".
    result = classify(_Stub("3576", ["Financing Receivables"]), "x", accession="x")
    assert result.ontology == "corporate"
    assert result.signal_names == ("captive_finance",)
