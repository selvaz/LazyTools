"""Classifying a filing's documents, against the shapes real filings actually have.

Every fixture below is copied from a live inventory read on 2026-08-29, because
the shapes that break a classifier are not the ones anyone would invent:

* Tesla's results 8-K carries **33 JPEGs** — the images inside the release —
  alongside ten XBRL artifacts, for two documents of substance.
* ``R1.htm`` is XBRL rendering output served as ``text/html``. A media-type check
  alone hands it back as a document.
* The ``description`` field merely repeats the type: Microsoft's earnings release
  describes itself as "EX-99.1". A rule looking for "Press Release" finds nothing.
* Banks file several EX-99 in one 8-K — JPMorgan a narrative plus a supplement,
  Bank of America a release plus a presentation plus a supplement — so "the
  exhibit" is not a thing that exists.
"""

from __future__ import annotations

from lazytools.connectors.edgar.documents import classify

MSFT_ACCN = "0001193125-26-323632"


def _doc(seq: str, type_: str, desc: str | None, filename: str, media: str) -> dict:
    return {"sequence": seq, "type": type_, "description": desc, "filename": filename,
            "media_type": media, "url": f"https://example.invalid/{filename}"}


# Microsoft's 8-K of 2026-07-29, trimmed to one of each kind.
MSFT_8K = [
    _doc("1", "8-K", "8-K", "msft-20260729.htm", "text/html"),
    _doc("2", "EX-99.1", "EX-99.1", "msft-ex99_1.htm", "text/html"),
    _doc("3", "EX-101.SCH", "XBRL TAXONOMY EXTENSION SCHEMA", "msft-20260729.xsd", "application/xml"),
    _doc("5", "XML", "IDEA: XBRL DOCUMENT", "R1.htm", "text/html"),
    _doc("6", "XML", "IDEA: XBRL DOCUMENT", "report.css", "application/octet-stream"),
    _doc("9", "XML", "IDEA: XBRL DOCUMENT", "FilingSummary.xml", "application/xml"),
    _doc("11", "JSON", "IDEA: XBRL DOCUMENT", "MetaLinks.json", "application/json"),
    _doc("12", "ZIP", "IDEA: XBRL DOCUMENT", f"{MSFT_ACCN}-xbrl.zip", "application/zip"),
    _doc("13", "XML", "IDEA: XBRL DOCUMENT", "msft-20260729_htm.xml", "application/xml"),
]


def _msft():
    return classify(MSFT_8K, accession=MSFT_ACCN, form="8-K")


# --- the release is found among the machinery ----------------------------- #


def test_the_release_is_the_first_candidate_and_the_body_the_second() -> None:
    result = _msft()
    assert [d.type for d in result.release_candidates] == ["EX-99.1", "8-K"]


def test_the_body_is_a_candidate_because_item_202_is_sometimes_stated_in_it() -> None:
    # Not every results 8-K puts the numbers in an exhibit, so dropping the
    # primary document would lose the answer on the filings that do not.
    assert _msft().primary is not None


def test_every_xbrl_artifact_is_excluded() -> None:
    result = _msft()
    assert {d.filename for d in result.documents if d.role == "xbrl"} == {
        "msft-20260729.xsd", "R1.htm", "report.css", "FilingSummary.xml",
        "MetaLinks.json", "msft-20260729_htm.xml",
    }


def test_the_renderer_html_is_not_mistaken_for_a_document() -> None:
    # R1.htm is text/html, so only its description gives it away.
    r1 = next(d for d in _msft().documents if d.filename == "R1.htm")
    assert r1.role == "xbrl"
    assert "edgar_renderer_artifact" in r1.signals
    assert r1 not in _msft().release_candidates


def test_the_zip_is_an_archive_not_an_exhibit() -> None:
    zipped = next(d for d in _msft().documents if d.filename.endswith(".zip"))
    assert zipped.role == "archive"


def test_a_single_exhibit_is_not_ambiguous() -> None:
    assert not _msft().ambiguous


def test_a_useless_description_does_not_prevent_classification() -> None:
    # "EX-99.1" as a description tells us nothing; the type does the work.
    release = _msft().release_candidates[0]
    assert release.description == "EX-99.1"
    assert release.signals == ("exhibit_99",)


# --- images ---------------------------------------------------------------- #


def test_the_releases_own_images_are_not_candidates() -> None:
    # Tesla's 8-K of 2026-07-22 carried 33 of these.
    docs = MSFT_8K + [
        _doc(str(20 + i), "GRAPHIC", None, f"exhibit991{i:03d}.jpg", "image/jpeg")
        for i in range(1, 34)
    ]
    result = classify(docs, accession="x", form="8-K")
    assert len([d for d in result.documents if d.role == "graphic"]) == 33
    assert [d.type for d in result.release_candidates] == ["EX-99.1", "8-K"]


# --- several exhibits: the case where "the exhibit" does not exist --------- #


BAC_8K = [
    _doc("1", "8-K", "8-K", "bac-20260714.htm", "text/html"),
    _doc("2", "EX-99.1", "EX-99.1", "bac06302026ex991.htm", "text/html"),
    _doc("3", "EX-99.2", "EX-99.2", "bac06302026ex992presentation.htm", "text/html"),
    _doc("4", "EX-99.3", "EX-99.3", "bac-06302026ex993supplement.htm", "text/html"),
]


def test_several_exhibits_are_reported_as_ambiguous_rather_than_narrowed() -> None:
    result = classify(BAC_8K, accession="bac", form="8-K")
    assert result.ambiguous
    assert [d.type for d in result.release_candidates] == ["EX-99.1", "EX-99.2", "EX-99.3", "8-K"]


def test_the_filename_carries_the_signal_the_description_does_not() -> None:
    result = classify(BAC_8K, accession="bac", form="8-K")
    by_type = {d.type: d.signals for d in result.release_candidates}
    assert "names_presentation" in by_type["EX-99.2"]
    assert "names_supplement" in by_type["EX-99.3"]
    assert "names_presentation" not in by_type["EX-99.1"]


def test_exhibits_are_ordered_by_their_number_not_alphabetically() -> None:
    shuffled = [BAC_8K[0], BAC_8K[3], BAC_8K[1], BAC_8K[2]]
    result = classify(shuffled, accession="bac", form="8-K")
    assert [d.type for d in result.release_candidates][:3] == ["EX-99.1", "EX-99.2", "EX-99.3"]


def test_a_double_digit_exhibit_sorts_after_the_single_digit_one() -> None:
    docs = [
        _doc("1", "EX-99.10", None, "ten.htm", "text/html"),
        _doc("2", "EX-99.2", None, "two.htm", "text/html"),
    ]
    result = classify(docs, accession="x", form="8-K")
    assert [d.type for d in result.release_candidates] == ["EX-99.2", "EX-99.10"]


# --- unreadable exhibits --------------------------------------------------- #


def test_a_pdf_exhibit_is_reported_unreadable_rather_than_offered() -> None:
    # This connector's media allowlist refuses a PDF, so a caller reaching one
    # needs a different extraction path -- not a retry, and not silence.
    docs = MSFT_8K + [_doc("14", "EX-99.2", None, "release.pdf", "application/pdf")]
    result = classify(docs, accession="x", form="8-K")
    assert [d.filename for d in result.unreadable] == ["release.pdf"]
    assert "release.pdf" not in {d.filename for d in result.release_candidates}
    assert "unreadable_media" in result.unreadable[0].signals


def test_an_unreadable_exhibit_still_counts_towards_ambiguity() -> None:
    # The tidy answer is the dangerous one. If the release is the PDF and the
    # HTML is a supplement, reporting a single unambiguous candidate makes the
    # caller read the supplement as the release with nothing warning it. An
    # exhibit we cannot open is still an exhibit that might be the answer.
    docs = MSFT_8K + [_doc("14", "EX-99.2", None, "release.pdf", "application/pdf")]
    assert classify(docs, accession="x", form="8-K").ambiguous


def test_a_spreadsheet_exhibit_is_unreadable_too() -> None:
    docs = [_doc("1", "EX-99.1", None, "financials.xlsx", "application/octet-stream")]
    result = classify(docs, accession="x", form="8-K")
    assert result.unreadable and not result.release_candidates


# --- other forms ----------------------------------------------------------- #


def test_the_primary_document_is_recognised_for_a_6k_not_only_an_8k() -> None:
    docs = [
        _doc("1", "6-K", "6-K", "sap-6k.htm", "text/html"),
        _doc("2", "EX-99.1", None, "sap-release.htm", "text/html"),
    ]
    result = classify(docs, accession="x", form="6-K")
    assert result.primary is not None and result.primary.type == "6-K"


def test_an_empty_inventory_yields_no_candidates_without_raising() -> None:
    result = classify([], accession="x", form="8-K")
    assert result.release_candidates == [] and result.documents == []


def test_a_document_with_no_type_is_not_promoted_to_an_exhibit() -> None:
    docs = [_doc("1", "", None, "mystery.htm", "text/html")]
    result = classify(docs, accession="x", form="8-K")
    assert result.documents[0].role == "other"
    assert result.release_candidates == []


# --- exhibits that are not the release ------------------------------------ #


def test_an_administrative_exhibit_is_never_a_release_candidate() -> None:
    # A filing-fee table is a readable XML exhibit. Without excluding it, it
    # sorts ahead of the primary document and is read as the release.
    docs = [
        _doc("1", "8-K", "8-K", "body.htm", "text/html"),
        _doc("2", "EX-FILING FEES", None, "fees.htm", "text/html"),
    ]
    result = classify(docs, accession="x", form="8-K")
    assert [d.type for d in result.release_candidates] == ["8-K"]
    assert not result.ambiguous


def test_a_material_agreement_never_outranks_the_earnings_release() -> None:
    # EX-10.1 is a real exhibit and sorts before EX-99.1 by bare number.
    docs = [
        _doc("1", "8-K", "8-K", "body.htm", "text/html"),
        _doc("2", "EX-10.1", None, "agreement.htm", "text/html"),
        _doc("3", "EX-99.1", None, "release.htm", "text/html"),
    ]
    result = classify(docs, accession="x", form="8-K")
    assert [d.type for d in result.release_candidates] == ["EX-99.1", "EX-10.1", "8-K"]


def test_an_exhibit_filed_as_an_image_is_reported_not_lost() -> None:
    # Classifying by media type first turned an EX-99.1 JPEG into a "graphic",
    # which appears in neither the candidates nor the unreadable list.
    docs = [_doc("1", "EX-99.1", None, "release.jpg", "image/jpeg")]
    result = classify(docs, accession="x", form="8-K")
    assert result.documents[0].role == "exhibit"
    assert [d.filename for d in result.unreadable] == ["release.jpg"]


def test_the_releases_embedded_images_are_still_excluded() -> None:
    # The GRAPHIC type, unlike an EX-* label, means exactly what it says.
    docs = [_doc("6", "GRAPHIC", None, "exhibit991001.jpg", "image/jpeg")]
    assert classify(docs, accession="x", form="8-K").documents[0].role == "graphic"


# --- exhibit ordering ------------------------------------------------------ #


def test_lettered_exhibit_components_do_not_collapse_onto_each_other() -> None:
    docs = [
        _doc("1", "EX-99.(b)(1)", None, "b.htm", "text/html"),
        _doc("2", "EX-99.(a)(1)", None, "a.htm", "text/html"),
    ]
    result = classify(docs, accession="x", form="8-K")
    assert [d.type for d in result.release_candidates] == ["EX-99.(a)(1)", "EX-99.(b)(1)"]


def test_a_lowercase_exhibit_type_is_read_the_same_way() -> None:
    docs = [_doc("1", "ex-99.1", None, "release.htm", "text/html")]
    result = classify(docs, accession="x", form="8-K")
    assert result.documents[0].role == "exhibit"
    assert result.documents[0].is_results_family


# --- amended filings ------------------------------------------------------- #


def test_an_amendments_primary_document_is_found_under_the_base_form() -> None:
    # An 8-K/A labels its own document "8-K"; matching only the exact form
    # leaves the amendment with no primary document at all.
    docs = [
        _doc("1", "8-K", "8-K", "body.htm", "text/html"),
        _doc("2", "EX-99.1", None, "release.htm", "text/html"),
    ]
    result = classify(docs, accession="x", form="8-K/A")
    assert result.primary is not None and result.primary.filename == "body.htm"


# --- falsifiability -------------------------------------------------------- #


def test_the_renderer_description_is_what_excludes_the_rendering_html() -> None:
    # R1.htm is text/html with no other tell. Delete the renderer-description
    # branch and this document becomes "other" -- but it must never be a
    # candidate, which is what this asserts.
    only_r1 = [_doc("5", "XML", "IDEA: XBRL DOCUMENT", "R1.htm", "text/html")]
    result = classify(only_r1, accession="x", form="8-K")
    assert result.documents[0].role == "xbrl"
    assert result.release_candidates == []
