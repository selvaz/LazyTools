"""Reading a filing's statements as the filer actually presented them.

The XBRL fact APIs answer "what value did this company report for this concept",
which is not the same question as "what does this income statement say". The
difference is where silent errors live. Cisco's FY2024 filing serves
``AmortizationOfIntangibleAssets`` at $698m entity-wide; the real total is
$1,653m, because $955m sits in cost of sales and reaches the API only as a
dimensioned fact the entity-wide endpoint never returns. A reader taking the
entity-wide number gets something with perfect provenance that is wrong by a
factor of two.

EDGAR renders those statements itself, as the ``R*.htm`` files indexed by
``FilingSummary.xml``. Every line carries what the facts alone do not: the label
the filer chose, the concept behind it, and where it sits. That is what makes an
aggregate checkable — the same concept appearing four times in one note comes
back four times, each with the label saying which slice it is.

**Scale is per row, not per table.** These tables are rendered in thousands or
millions while XBRL facts are in units, so a multiplier has to be applied. But a
header reading "$ in Millions, except per share data" means exactly what it
says: multiplying an EPS of 0.47 by a million gives 470,000, a number that looks
like data and is nonsense. So the money scale reaches monetary rows only;
per-share rows are left in units, share counts take the share scale, and a row
whose scale cannot be established comes back ``None`` rather than wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

from lazytools.connectors.edgar.client import EdgarService

#: The index of rendered reports that EDGAR puts in every modern filing.
SUMMARY_FILENAME = "FilingSummary.xml"
#: The MenuCategory the primary financial statements are filed under.
STATEMENTS_CATEGORY = "Statements"

_REPORT_BLOCK = re.compile(r"<Report[^>]*>(.*?)</Report>", re.S | re.I)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<(t[dh])\b([^>]*)>(.*?)</\1>", re.S | re.I)
_CONCEPT = re.compile(r"defref_([A-Za-z0-9_\-]+)")
_CLASS = re.compile(r"""class\s*=\s*["']?([^"'>]*)""", re.I)
_TAGS = re.compile(r"<[^>]+>")

_SCALE_WORDS = {"units": 1, "thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}
_MONEY_SCALE = re.compile(r"\$\s*in\s+(units|thousands|millions|billions)", re.I)
_SHARE_SCALE = re.compile(r"shares?\s+in\s+(units|thousands|millions|billions)", re.I)
_BARE_SCALE = re.compile(r"\bin\s+(units|thousands|millions|billions)\b", re.I)

#: Concepts that hold a table together and never label anything. A note that
#: splits a total by axis renders "[Line Items]" between the member name and its
#: figures, so without this the section of every dimensioned row would be that
#: string rather than "Cost of sales".
_STRUCTURAL_SUFFIXES = ("LineItems", "Table", "Domain")
#: A per-share row is rendered in actual currency whatever the table's money
#: scale says — which is why "except per share data" appears in so many headers.
_PER_SHARE = re.compile(r"per\s*share|pershare", re.I)
#: A row counting shares takes the share scale, not the money scale.
_SHARE_COUNT = re.compile(r"\bshares\b|sharesoutstanding|sharesissued", re.I)


@dataclass(frozen=True)
class ReportRef:
    """One rendered report inside a filing, from ``FilingSummary.xml``."""

    filename: str
    short_name: str
    category: str

    @property
    def is_primary_statement(self) -> bool:
        """A face financial statement rather than a note, cover or detail.

        Parentheticals are excluded: they carry share counts and par values, not
        statement lines. The test reads the filer's own title, so it follows a
        convention rather than a guarantee.
        """
        return self.category == STATEMENTS_CATEGORY and "parenthetical" not in self.short_name.lower()


@dataclass(frozen=True)
class StatementLine:
    """One presented line: what it is called, what it is tagged as, its values.

    ``values`` is aligned to :attr:`RenderedStatement.columns`. ``None`` marks a
    cell the filer left blank — which is not zero — or a value whose scale could
    not be established.
    """

    label: str
    concept: str | None
    values: tuple[float | None, ...]
    #: True when the row carries no values at all, so it labels its neighbours
    #: rather than reporting anything.
    is_label_only: bool
    section: str | None

    @property
    def tag(self) -> str | None:
        """The concept without its taxonomy prefix, or ``None``."""
        if self.concept is None:
            return None
        return self.concept.split("_", 1)[1] if "_" in self.concept else self.concept


@dataclass(frozen=True)
class RenderedStatement:
    """One statement as presented, with each row's scale resolved."""

    report: ReportRef
    title: str
    columns: tuple[str, ...]
    lines: tuple[StatementLine, ...]
    #: The money multiplier the header declared, or ``None`` when it declared
    #: none. Kept because reconciling figures from a table rendered in millions
    #: needs to know that: with no tolerance, a total that does not sum exactly
    #: because of the table's own rounding reads as a contradiction.
    money_scale: int | None = None

    def by_tag(self, tag: str) -> list[StatementLine]:
        """Every presented line tagged with ``tag``, in presentation order.

        More than one is normal and is the point: a concept can appear several
        times in one report, and only the section says what each occurrence is.
        """
        return [line for line in self.lines if line.tag == tag]


def list_reports(client: EdgarService, cik: str, accession: str) -> list[ReportRef]:
    """Every rendered report in a filing, in presentation order, from its index.

    Raises:
        ValueError: when the filing has no readable ``FilingSummary.xml``.
            Filings older than EDGAR's renderer have no rendered statements, and
            that is worth saying rather than returning an empty list for a
            caller to misread as "this filing has no balance sheet".
    """
    content = client.get_filing_document(cik, accession, SUMMARY_FILENAME, raw=True).get("content") or ""
    if not content.strip():
        raise ValueError(
            f"filing {accession} has no readable {SUMMARY_FILENAME}; filings before "
            "EDGAR's renderer have no rendered statements to read"
        )
    reports = [
        ReportRef(filename=name, short_name=_field(block, "ShortName"),
                  category=_field(block, "MenuCategory"))
        for block in _REPORT_BLOCK.findall(content)
        if (name := _field(block, "HtmlFileName") or _field(block, "XmlFileName"))
    ]
    if not reports:
        raise ValueError(f"{SUMMARY_FILENAME} for {accession} listed no reports; it may not have parsed")
    return reports


def read_statement(
    client: EdgarService, cik: str, accession: str, report: ReportRef
) -> RenderedStatement:
    """Fetch and parse one rendered report.

    Fetched raw: each line's concept lives in an HTML attribute, so tag-stripped
    text would deliver the numbers and discard what they are.
    """
    content = client.get_filing_document(cik, accession, report.filename, raw=True).get("content") or ""
    return parse_statement(content, report=report)


def parse_statement(html: str, *, report: ReportRef) -> RenderedStatement:
    """Parse one ``R*.htm`` table. Pure — no network."""
    title = ""
    header_rows: list[list[str]] = []
    raw_lines: list[tuple[str, str | None, tuple[str, ...]]] = []

    for row in _ROW.findall(html):
        cells = _CELL.findall(row)
        if not cells:
            continue
        kinds = {kind.lower() for kind, _, _ in cells}
        classes = _classes(cells[0][1])

        if "tl" in classes:
            title = title or _plain(cells[0][2])
            header_rows.append([_plain(c[2]) for c in cells[1:]])
        elif kinds == {"th"}:
            header_rows.append([_plain(c[2]) for c in cells])
        elif "pl" in classes:
            match = _CONCEPT.search(cells[0][1] + cells[0][2])
            raw_lines.append((_plain(cells[0][2]),
                              match.group(1) if match else None,
                              tuple(_plain(c[2]) for c in cells[1:])))

    # The LAST header row carries the period labels. Earlier rows span them
    # ("12 Months Ended" above three dates), so taking the first leaves one
    # column label standing over three columns of values.
    columns = next((tuple(r) for r in reversed(header_rows) if len(r) > 1),
                   tuple(header_rows[-1]) if header_rows else ())
    money, shares = _scale(title, "money"), _scale(title, "shares")

    lines: list[StatementLine] = []
    section: str | None = None
    for label, concept, texts in raw_lines:
        numbers = [_number(t) for t in texts]
        label_only = all(n is None for n in numbers)
        scale = _row_scale(label, concept, money, shares)
        lines.append(StatementLine(
            label=label,
            concept=concept,
            values=tuple(None if n is None or scale is None else n * scale for n in numbers),
            is_label_only=label_only,
            section=None if label_only else section,
        ))
        if label_only and not _is_structural(concept):
            section = label
    return RenderedStatement(report=report, title=title, columns=columns,
                             lines=tuple(lines), money_scale=money)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _row_scale(label: str, concept: str | None, money: int | None, shares: int | None) -> int | None:
    """The multiplier for ONE row, or ``None`` to withhold its values.

    One table can mix a money scale, a share scale and per-share amounts
    rendered in actual currency. Applying the header's money multiplier to every
    row is how an EPS of 0.47 becomes 470,000.
    """
    text = f"{label} {concept or ''}"
    if _PER_SHARE.search(text):
        return 1
    if _SHARE_COUNT.search(text):
        return shares
    return money


def _scale(note: str, kind: str) -> int | None:
    """The money or share multiplier stated in a table's header.

    Read separately because a header carries both: "shares in Millions, $ in
    Thousands" scales currency by a thousand and share counts by a million, so
    whichever is matched first is the wrong answer for the other.
    """
    specific = (_MONEY_SCALE if kind == "money" else _SHARE_SCALE).search(note)
    if specific:
        return _SCALE_WORDS[specific.group(1).lower()]
    if kind == "money":
        bare = _BARE_SCALE.search(note)
        if bare:
            return _SCALE_WORDS[bare.group(1).lower()]
        if "$" in note:
            # A money column with no stated scale: EDGAR renders it in units.
            return 1
    return None


def _is_structural(concept: str | None) -> bool:
    return bool(concept) and concept.endswith(_STRUCTURAL_SUFFIXES)


def _classes(attributes: str) -> set[str]:
    match = _CLASS.search(attributes)
    return set(match.group(1).split()) if match else set()


def _field(block: str, name: str) -> str:
    match = re.search(rf"<{name}>(.*?)</{name}>", block, re.S | re.I)
    return unescape(match.group(1)).strip() if match else ""


def _plain(cell: str) -> str:
    return " ".join(unescape(_TAGS.sub(" ", cell)).replace("\xa0", " ").split())


def _number(text: str) -> float | None:
    cleaned = text.replace("$", "").replace(",", "").strip()
    if not cleaned or cleaned in {"-", "—", "–"}:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()").rstrip("%").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
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
