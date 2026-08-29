"""Finding the earnings release inside a filing that is mostly not the release.

A results 8-K is a small document surrounded by machinery. Measured against real
filings on 2026-08-29: Microsoft's carries 10 documents, Apple's 15, and Tesla's
48 — of which **33 are JPEGs**, the images embedded in the release itself. The
rest is XBRL apparatus: taxonomy linkbases, a rendering stylesheet, a JavaScript
file, a summary XML, a metadata JSON, and a zip of all of it.

Two findings shape everything here.

**The description field is worthless.** In all three filings it merely repeats
the type: the earnings release describes itself as "EX-99.1". Any rule keyed on
a description saying "Press Release" fails on the most common filings there are.

**Media type alone is not readability.** ``R1.htm`` — XBRL rendering output — is
served as ``text/html`` and would sail through a naive text filter to arrive as
a page of viewer scaffolding pretending to be a document.

So classification is by **exclusion**: identify the machinery, and what remains
is the filing and its real exhibits. What this module never does is declare one
document to be *the* release. It ranks candidates and reports when more than one
substantive exhibit exists — a filing can carry the release, the slide deck and
a financial supplement, and choosing among them is a judgement about what was
asked for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from lazytools.connectors.edgar.client import TEXT_MEDIA, EdgarService

#: What a document is doing in the filing.
DocumentRole = Literal["primary", "exhibit", "graphic", "xbrl", "archive", "other"]

#: Exhibit-type prefixes that are XBRL taxonomy apparatus, not content.
_XBRL_EXHIBIT_PREFIXES = ("EX-101", "EX-99.SCH", "EX-100")
#: Exhibit families that are filing administration, never a results document.
#: An ``EX-FILING FEES`` table is a readable XML exhibit, so without this it
#: would rank ahead of the primary document and be read as the release.
_ADMIN_EXHIBIT_PREFIXES = (
    "EX-FILING", "EX-107", "EX-24", "EX-23", "EX-21", "EX-31", "EX-32", "EX-95",
)
#: The exhibit family that carries results material. Not a filter -- an unusual
#: filing may put the release elsewhere -- but the one that ranks first.
_RESULTS_EXHIBIT_PREFIX = "EX-99"
#: Document types EDGAR uses for its own generated artifacts.
_MACHINERY_TYPES = frozenset({"XML", "JSON", "ZIP", "EXCEL", "GRAPHIC"})
#: The description EDGAR stamps on every artifact its renderer produces. This is
#: the one description that carries information, and it is a negative signal.
_RENDERER_DESCRIPTION = "IDEA: XBRL"
#: Filename endings that are XBRL instance/linkbase files whatever their type.
_XBRL_SUFFIXES = (
    "_htm.xml", "_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", ".xsd",
)
#: Words that, when a description does carry them, say what a document is.
#: Rare in practice — see the module docstring — but free to check.
_RELEASE_WORDS = re.compile(
    r"press\s*release|earnings|results|financial\s+statements?|news\s*release", re.I)
_DECK_WORDS = re.compile(r"present|slide|deck|webcast|script|transcript", re.I)
_SUPPLEMENT_WORDS = re.compile(r"supplement|financial\s+data|statistical|workbook", re.I)


@dataclass(frozen=True)
class DocumentRef:
    """One document in a filing, with why it might or might not be the release."""

    sequence: str | None
    type: str
    description: str | None
    filename: str
    media_type: str
    url: str
    role: DocumentRole
    #: Whether this connector can turn it into text at all. A PDF or a
    #: spreadsheet exhibit is refused by the client's media allowlist, so a
    #: caller reaching one needs a different extraction path, not a retry.
    readable: bool
    signals: tuple[str, ...]

    @property
    def is_results_family(self) -> bool:
        """``EX-99.*`` — the family results material is filed under."""
        return self.type.upper().startswith(_RESULTS_EXHIBIT_PREFIX)

    @property
    def exhibit_number(self) -> tuple[str, ...]:
        """``EX-99.2`` → ``("0099", "0002")``; orders exhibits as filed.

        Components are zero-padded strings rather than ints so that ``EX-99.2``
        precedes ``EX-99.10`` (which plain string order would not) while a
        lettered component like ``EX-99.(a)(1)`` still sorts, and sorts apart
        from ``EX-99.(b)(1)`` instead of collapsing onto it.
        """
        parts = re.findall(r"[0-9]+|[a-z]+", self.type.lower().removeprefix("ex-"))
        return tuple(p.zfill(4) if p.isdigit() else p for p in parts) or ("zzzz",)


@dataclass(frozen=True)
class DocumentSet:
    """Every document in one filing, sorted into what it is.

    ``release_candidates`` is ordered best-first but is **not** a decision:
    ``ambiguous`` says whether more than one substantive exhibit is in play, and
    a caller that treats the first entry as the release when ``ambiguous`` is
    true has made a choice this module declined to make for it.
    """

    accession: str
    documents: list[DocumentRef]
    release_candidates: list[DocumentRef]
    unreadable: list[DocumentRef]

    @property
    def ambiguous(self) -> bool:
        """More than one substantive exhibit could be the release.

        Counts unreadable exhibits too. Leaving them out looks tidy and is the
        more dangerous answer: a filing whose release is a PDF and whose
        supplement is HTML would report a single unambiguous candidate, and the
        caller would read the supplement as the release with nothing warning it.
        An exhibit we cannot open is still an exhibit that might be the answer.
        """
        exhibits = [d for d in (*self.release_candidates, *self.unreadable)
                    if d.role == "exhibit"]
        return len(exhibits) > 1

    @property
    def primary(self) -> DocumentRef | None:
        """The filing's own document — the 8-K body, not an exhibit."""
        return next((d for d in self.documents if d.role == "primary"), None)


def classify(
    documents: list[dict[str, Any]], *, accession: str, form: str,
) -> DocumentSet:
    """Sort one filing's document inventory without fetching anything.

    Args:
        documents: from ``EdgarService.list_filing_documents``.
        accession: the filing these belong to, carried through for citation.
        form: the filing's form type, e.g. ``"8-K"``. A document whose own type
            equals the form is the primary document; matching on that rather
            than on sequence 1 survives a filing whose ordering is unusual.

    Pure: no network, no LLM, no heuristics beyond the filing's own metadata.
    """
    refs = [_classify_one(d, form=form) for d in documents]
    substantive = [d for d in refs
                   if d.role in ("exhibit", "primary")
                   and "administrative_exhibit" not in d.signals]
    candidates = [d for d in substantive if d.readable]
    # Ordering, outermost key first:
    #   exhibits before the primary document -- Item 2.02 usually points at an
    #     exhibit, and the 8-K body is the fallback for filings that state the
    #     results in it directly;
    #   EX-99 before other exhibit families -- an EX-10.1 material agreement is
    #     a real exhibit and is never the earnings release, so ordering by bare
    #     exhibit number would put it first;
    #   then the exhibit's own number, then the filename to break ties.
    candidates.sort(key=lambda d: (d.role != "exhibit", not d.is_results_family,
                                   d.exhibit_number, d.filename))
    unreadable = [d for d in substantive if not d.readable]
    return DocumentSet(
        accession=accession,
        documents=refs,
        release_candidates=candidates,
        unreadable=unreadable,
    )


def fetch_documents(
    client: EdgarService, cik: str, accession: str, *, form: str,
) -> DocumentSet:
    """:func:`classify` over a live inventory. One request."""
    return classify(
        list(client.list_filing_documents(cik, accession)), accession=accession, form=form,
    )


def _classify_one(document: dict[str, Any], *, form: str) -> DocumentRef:
    doc_type = str(document.get("type") or "").strip()
    description = document.get("description")
    filename = str(document.get("filename") or "")
    media = str(document.get("media_type") or "")
    upper_type = doc_type.upper()
    signals: list[str] = []

    role: DocumentRole = "other"
    # The filer's OWN exhibit label comes first, before any inference from the
    # file's shape. An EX-99.1 filed as a JPEG or a PDF is still the exhibit the
    # filer designated; classifying it by its media type instead drops it out of
    # both the candidate list and the unreadable list, and it disappears.
    if upper_type.startswith(_XBRL_EXHIBIT_PREFIXES):
        role = "xbrl"
        signals.append("xbrl_taxonomy_exhibit")
    elif upper_type.startswith("EX-"):
        role = "exhibit"
        signals.append("exhibit_99" if upper_type.startswith(_RESULTS_EXHIBIT_PREFIX) else "exhibit")
        if upper_type.startswith(_ADMIN_EXHIBIT_PREFIXES):
            signals.append("administrative_exhibit")
    elif upper_type == "GRAPHIC" or media.startswith("image/"):
        role = "graphic"
        signals.append("image")
    elif upper_type == "ZIP" or media == "application/zip":
        role = "archive"
        signals.append("archive")
    elif str(description or "").startswith(_RENDERER_DESCRIPTION) or filename.lower().endswith(
        _XBRL_SUFFIXES
    ):
        # EDGAR's own renderer stamps this description on everything it
        # generates, including an R1.htm served as text/html that a media-type
        # check alone would happily hand back as a document.
        role = "xbrl"
        signals.append("edgar_renderer_artifact")
    elif upper_type in _MACHINERY_TYPES:
        role = "xbrl"
        signals.append("edgar_generated")
    elif upper_type == form.upper() or upper_type == form.upper().removesuffix("/A"):
        # An amended filing labels its primary document with the BASE form: an
        # 8-K/A's own document is typed "8-K". Matching only the exact form
        # leaves an amendment with no primary document at all.
        role = "primary"
        signals.append("primary_document")

    text = f"{description or ''} {filename}"
    if _RELEASE_WORDS.search(text):
        signals.append("names_results")
    if _DECK_WORDS.search(text):
        signals.append("names_presentation")
    if _SUPPLEMENT_WORDS.search(text):
        signals.append("names_supplement")

    readable = media in TEXT_MEDIA
    if not readable and role in ("exhibit", "primary"):
        # Worth saying out loud rather than leaving as a silent False: a foreign
        # issuer's 6-K exhibit is often a PDF, and this connector's media
        # allowlist refuses it. That is a missing capability, not a bad filing.
        signals.append("unreadable_media")

    return DocumentRef(
        sequence=str(document["sequence"]) if document.get("sequence") else None,
        type=doc_type,
        description=str(description) if description else None,
        filename=filename,
        media_type=media,
        url=str(document.get("url") or ""),
        role=role,
        readable=readable,
        signals=tuple(signals),
    )


__all__ = [
    "DocumentRef",
    "DocumentRole",
    "DocumentSet",
    "classify",
    "fetch_documents",
]
