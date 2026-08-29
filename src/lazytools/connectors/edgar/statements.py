"""Reading a filing's statements as the filer actually presented them.

The XBRL fact APIs answer "what value did this company report for this concept",
which is not the same question as "what does this company's income statement
say". The difference is where silent errors live. Cisco's FY2024 filing tags
``DepreciationDepletionAndAmortization`` at $700m and
``AmortizationOfIntangibleAssets`` at $698m as separate entity-wide facts, and a
reader that takes the first as total D&A understates it by two thirds — because
a further $955m of intangible amortization sits inside cost of sales, and no
entity-wide fact says so. The rendered statement does.

EDGAR generates those rendered statements itself, as the ``R*.htm`` files
indexed by ``FilingSummary.xml``. Every line carries three things the facts
alone do not:

* the **label the filer chose** ("Total cost of sales", "GROSS MARGIN"),
* the **concept** behind it, and
* **where it sits** — which statement, and under which section heading.

That is what makes an aggregate checkable: a total can be reconciled against the
components presented beneath it, and a concept can be located in the statement
rather than assumed to be entity-wide.

**The scale is a trap and is handled explicitly.** These tables are rendered in
thousands or millions — Cisco's says "$ in Millions" — while XBRL facts are in
units. A parser that returns 56,654 next to a fact of 56,654,000,000 has
produced two numbers that disagree by a factor of a million and look equally
valid. :class:`RenderedStatement` records the multiplier and returns values in
UNITS, so the two are directly comparable; when the scale cannot be read, values
come back ``None`` rather than unscaled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Any

from lazytools.connectors.edgar.client import EdgarService

#: The index of rendered reports that EDGAR puts in every modern filing.
SUMMARY_FILENAME = "FilingSummary.xml"
#: The MenuCategory the primary financial statements are filed under.
STATEMENTS_CATEGORY = "Statements"

_REPORT_BLOCK = re.compile(r"<Report[^>]*>(.*?)</Report>", re.S | re.I)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh]\b([^>]*)>(.*?)</t[dh]>", re.S | re.I)
_CONCEPT = re.compile(r"defref_([A-Za-z0-9_\-]+)")
_CLASS = re.compile(r'class="([^"]*)"')
_TAGS = re.compile(r"<[^>]+>")
#: "$ in Millions", "shares in Thousands, $ in Millions", "In Thousands".
_SCALE_WORDS = {"units": 1, "thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}
_MONEY_SCALE = re.compile(r"\$\s*in\s+(units|thousands|millions|billions)", re.I)
_BARE_SCALE = re.compile(r"\bin\s+(units|thousands|millions|billions)\b", re.I)


@dataclass(frozen=True)
class ReportRef:
    """One rendered report inside a filing, from ``FilingSummary.xml``."""

    filename: str
    short_name: str
    category: str
    position: int

    @property
    def is_primary_statement(self) -> bool:
        """A face financial statement rather than a note, cover or detail.

        Parentheticals are excluded: they carry share counts and par values, not
        statement lines, and treating one as the balance sheet finds nothing.
        """
        return self.category == STATEMENTS_CATEGORY and "parenthetical" not in self.short_name.lower()


@dataclass(frozen=True)
class StatementLine:
    """One presented line: what it is called, what it is tagged as, its values.

    ``values`` is aligned to :attr:`RenderedStatement.columns` and is already in
    UNITS. ``None`` marks a cell the filer left blank, which is not zero.
    """

    label: str
    concept: str | None
    values: tuple[float | None, ...]
    is_abstract: bool
    is_emphasised: bool
    section: str | None

    @property
    def tag(self) -> str | None:
        """The concept without its taxonomy prefix, or ``None``."""
        if self.concept is None:
            return None
        return self.concept.split("_", 1)[1] if "_" in self.concept else self.concept

    @property
    def taxonomy(self) -> str | None:
        """``us-gaap``, ``ifrs-full``, or the filer's own namespace prefix."""
        if self.concept is None:
            return None
        return self.concept.split("_", 1)[0] if "_" in self.concept else None

    @property
    def is_extension(self) -> bool:
        """Tagged with the filer's OWN concept, which the XBRL APIs never serve.

        Worth knowing on sight: a line that matters and is an extension is one
        the fact APIs cannot reach at all, so the rendered statement is not a
        cross-check for it — it is the only source.
        """
        return self.taxonomy not in (None, "us-gaap", "ifrs-full", "dei", "srt")


@dataclass(frozen=True)
class RenderedStatement:
    """One statement as presented, with its scale resolved."""

    report: ReportRef
    title: str
    columns: tuple[str, ...]
    lines: tuple[StatementLine, ...]
    #: Multiplier applied to every value, or ``None`` when it could not be read.
    scale: int | None
    scale_note: str

    def by_tag(self, tag: str) -> list[StatementLine]:
        """Every presented line tagged with ``tag``, in presentation order.

        More than one is normal and is the point: a concept can appear in two
        sections of one statement, and only the section says what it means.
        """
        return [ln for ln in self.lines if ln.tag == tag]

    def sections(self) -> list[str]:
        """The section headings, in order."""
        out: list[str] = []
        for ln in self.lines:
            if ln.is_abstract and ln.label not in out:
                out.append(ln.label)
        return out


def list_reports(client: EdgarService, cik: str, accession: str) -> list[ReportRef]:
    """Every rendered report in a filing, from its own index.

    Raises:
        ValueError: when the filing has no readable ``FilingSummary.xml``. Older
            filings predate the renderer, and that is a fact about the filing
            worth surfacing rather than an empty list to misread as "no
            statements".
    """
    document = client.get_filing_document(cik, accession, SUMMARY_FILENAME)
    content = document.get("content") or ""
    if not content.strip():
        raise ValueError(
            f"filing {accession} has no readable {SUMMARY_FILENAME}; filings before "
            "EDGAR's renderer have no rendered statements to read"
        )
    reports: list[ReportRef] = []
    for position, block in enumerate(_REPORT_BLOCK.findall(content), start=1):
        filename = _field(block, "HtmlFileName") or _field(block, "XmlFileName")
        if not filename:
            continue
        reports.append(
            ReportRef(
                filename=filename,
                short_name=_field(block, "ShortName") or "",
                category=_field(block, "MenuCategory") or "",
                position=position,
            )
        )
    if not reports:
        raise ValueError(f"{SUMMARY_FILENAME} for {accession} listed no reports; it may not have parsed")
    return reports


def read_statement(
    client: EdgarService, cik: str, accession: str, report: ReportRef
) -> RenderedStatement:
    """Fetch and parse one rendered report.

    The document is fetched RAW: the concept behind each line lives in an HTML
    attribute, so tag-stripped text would deliver the numbers and discard what
    they are.
    """
    document = client.get_filing_document(cik, accession, report.filename, raw=True)
    return parse_statement(document.get("content") or "", report=report)


def parse_statement(html: str, *, report: ReportRef) -> RenderedStatement:
    """Parse one ``R*.htm`` table. Pure — no network."""
    rows = _ROW.findall(html)
    title, scale_note, columns = "", "", []
    lines: list[StatementLine] = []
    section: str | None = None

    for row in rows:
        cells = _CELL.findall(row)
        if not cells:
            continue
        head = _plain(cells[0][1])
        classes = _CLASS.search(cells[0][0])
        first_class = classes.group(1) if classes else ""

        if "tl" in first_class.split():
            # The header cell carries the statement's own title and its scale.
            title = title or head
            scale_note = scale_note or head
            columns.extend(_plain(c[1]) for c in cells[1:] if _plain(c[1]))
            continue
        if not columns and all("th" in (_CLASS.search(c[0]).group(1) if _CLASS.search(c[0]) else "")
                               for c in cells):
            columns.extend(_plain(c[1]) for c in cells if _plain(c[1]))
            continue
        if "pl" not in first_class.split():
            continue

        concept_match = _CONCEPT.search(cells[0][0] + cells[0][1])
        concept = concept_match.group(1) if concept_match else None
        values_here = tuple(_number(c[1], c[0]) for c in cells[1:])
        # A row carrying no values labels the rows around it rather than
        # reporting anything. That covers more than "*Abstract": a note that
        # breaks a total down by axis renders the member name ("Cost of sales",
        # "Operating expenses", "Total") as its own value-less row, and those
        # are precisely the labels that say which slice the next figures are.
        # Cisco's amortisation schedule is unreadable without them: the same
        # concept appears four times with four different meanings.
        is_abstract = all(v is None for v in values_here)
        line = StatementLine(
            label=head,
            concept=concept,
            values=values_here,
            is_abstract=is_abstract,
            is_emphasised="<strong>" in cells[0][1].lower(),
            section=None if is_abstract else section,
        )
        if is_abstract and not _is_structural(concept):
            section = head
        lines.append(line)

    scale = _scale(scale_note)
    if scale is not None and scale != 1:
        lines = [
            StatementLine(
                label=ln.label, concept=ln.concept,
                values=tuple(None if v is None else v * scale for v in ln.values),
                is_abstract=ln.is_abstract, is_emphasised=ln.is_emphasised, section=ln.section,
            )
            for ln in lines
        ]
    elif scale is None:
        # Unscaled values are a factor of a thousand or a million away from the
        # facts they would be compared against. Withheld rather than returned.
        lines = [
            StatementLine(
                label=ln.label, concept=ln.concept, values=tuple(None for _ in ln.values),
                is_abstract=ln.is_abstract, is_emphasised=ln.is_emphasised, section=ln.section,
            )
            for ln in lines
        ]
    return RenderedStatement(
        report=report, title=title, columns=tuple(columns), lines=tuple(lines),
        scale=scale, scale_note=scale_note,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
#: Concepts that exist to hold a table together and never label anything.
#: A note that splits a total by axis renders "[Line Items]" between the member
#: name and its figures, so without this the section of every dimensioned row is
#: the same meaningless string instead of "Cost of sales".
_STRUCTURAL_SUFFIXES = ("LineItems", "Table", "Domain")


def _is_structural(concept: str | None) -> bool:
    return bool(concept) and concept.endswith(_STRUCTURAL_SUFFIXES)


def _field(block: str, name: str) -> str:
    match = re.search(rf"<{name}>(.*?)</{name}>", block, re.S | re.I)
    return unescape(match.group(1)).strip() if match else ""


def _plain(cell: str) -> str:
    return " ".join(unescape(_TAGS.sub(" ", cell)).replace("\xa0", " ").split())


def _scale(note: str) -> int | None:
    """The money multiplier from a header like "$ in Millions".

    The money scale is read first and separately: a header saying "shares in
    Millions, $ in Thousands" carries two, and taking whichever appears first
    scales every currency figure by a thousand too much.
    """
    money = _MONEY_SCALE.search(note)
    if money:
        return _SCALE_WORDS[money.group(1).lower()]
    bare = _BARE_SCALE.search(note)
    if bare and "share" not in note.lower():
        return _SCALE_WORDS[bare.group(1).lower()]
    if note and "$" in note:
        # A money column with no stated scale: EDGAR renders those in units.
        return 1
    return None


def _number(cell: str, attributes: str) -> float | None:
    text = _plain(cell).replace("$", "").replace(",", "").strip()
    if not text or text in {"-", "—", "–"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").rstrip("%").strip()
    try:
        value = float(text)
    except ValueError:
        return None
    _ = attributes  # class carries no sign information EDGAR does not also render
    return -value if negative else value


__all__ = [
    "STATEMENTS_CATEGORY",
    "SUMMARY_FILENAME",
    "RenderedStatement",
    "ReportRef",
    "StatementLine",
    "list_reports",
    "parse_statement",
    "read_statement",
]
