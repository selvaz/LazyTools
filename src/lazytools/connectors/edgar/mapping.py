"""Deciding which presented line is which normalised element.

This is the one part of normalisation that is genuinely semantic, and the one a
lookup table does worst. A table of XBRL concepts has to be guessed filer by
filer: Cisco tags its short-term debt ``DebtCurrent``, Walmart tags total D&A
``DepreciationAmortizationAndAccretionNet``, and a table that knows neither
returns nothing while the figures sit in plain sight on the face of the
statements, labelled in English by the filer.

So the mapping is proposed by a model. What the model may return is deliberately
narrow: **a reference to a line, never a number**. It names the statement, the
label and the column; the value is then read out of the parsed statement by
code. A fabricated figure is therefore not something to detect — it is
something the interface cannot express.

Everything the model proposes is still a proposal. Scale, period selection,
reconciliation and the state a figure ends up with are decided afterwards by
code that does not consult it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from lazytools.connectors.edgar.statements import RenderedStatement
from lazytools.financials.normalised import COMPUTED_ELEMENTS, ELEMENTS

#: Cheap by design: this is a labelling job over a few dozen short strings, not
#: an analysis. The caller overrides it when it wants something else.
DEFAULT_MODEL = "deepseek-v4-flash"

#: The rules that survive as a literal, because they describe THIS call's
#: contract rather than how filings behave: what to return and in what form.
#: Everything about how filings behave comes from the instruction library, where
#: it can be read, cited and corrected by someone who is not editing Python.
_TASK = """You map lines of a company's own financial statements onto a fixed set of
normalised element ids. You are labelling, not analysing.

Refer to a line by its statement and its exact label. If an element is not
present in what you were given, say so and say why: do not infer it from a
related line, and do not compute it. Prefer the consolidated total over any
segment or product breakdown. Return one entry per element you can place, and
one absence per element you cannot."""


def _system() -> str:
    """The mapping call's instructions: this call's contract, then the library.

    Imported inside the function because the library lives in the skills
    package and the skills package builds agents over these connectors;
    importing it at module scope closes that circle.
    """
    from lazytools.skills.retriever import mapping_instructions

    return f"{_TASK}\n\n{mapping_instructions()}"


@dataclass(frozen=True)
class LineRef:
    """A reference to one presented line. Carries no value on purpose."""

    element_id: str
    statement: str
    label: str


@dataclass(frozen=True)
class Absence:
    """An element the model could not place, and why."""

    element_id: str
    reason: str


@dataclass(frozen=True)
class Mapping:
    """What the model proposed. Nothing here has been checked yet."""

    refs: tuple[LineRef, ...]
    absences: tuple[Absence, ...]
    #: Proposals discarded before they were returned, with the reason. A model
    #: naming an element that does not exist is a fact worth keeping.
    rejected: tuple[str, ...] = ()


def statements_as_prompt(statements: list[RenderedStatement], *, column: int) -> str:
    """The statements as the model sees them: labels and concepts, no values.

    Values are withheld from the prompt as well as from the answer. The model's
    job is to recognise what a line IS, and a figure it never saw is a figure it
    cannot anchor a wrong answer to.
    """
    payload = [
        {
            "statement": s.title,
            "lines": [
                {"label": line.label, "concept": line.tag}
                for line in s.lines
                if not line.is_label_only and column < len(line.values)
                and line.values[column] is not None
            ],
        }
        for s in statements
    ]
    return json.dumps(payload, indent=1, ensure_ascii=False)


def elements_as_prompt() -> str:
    """The PRESENTED elements and their meanings — what makes them mappable.

    Computed elements are not offered. Free cash flow is not a line a filer
    shows, and accepting a model's claim to have found one replaces the
    calculation with a relabelled cash-flow line that every figure
    downstream then inherits.
    """
    return "\n".join(
        f"- {key}: {spec.meaning}"
        for key, spec in ELEMENTS.items()
        if key not in COMPUTED_ELEMENTS
    )


def parse_mapping(payload: Any) -> Mapping:
    """Turn a model's answer into a :class:`Mapping`, dropping what it may not say.

    An entry naming an element outside the registry is rejected rather than
    accepted, and an entry carrying a value is stripped of it — the model does
    not get to supply figures whatever it returns.
    """
    refs: list[LineRef] = []
    absences: list[Absence] = []
    rejected: list[str] = []

    for entry in _as_list(payload, "mapped", "refs", "elements"):
        element_id = str(entry.get("element_id") or "").strip()
        if element_id not in ELEMENTS:
            rejected.append(f"{element_id or '(unnamed)'}: not an element of the base")
            continue
        if element_id in COMPUTED_ELEMENTS:
            rejected.append(f"{element_id}: computed from other elements, never read "
                            "from a statement")
            continue
        label = str(entry.get("label") or "").strip()
        if not label:
            rejected.append(f"{element_id}: named no line label")
            continue
        refs.append(LineRef(element_id=element_id,
                            statement=str(entry.get("statement") or "").strip(),
                            label=label))

    for entry in _as_list(payload, "absent", "absences", "missing"):
        element_id = str(entry.get("element_id") or "").strip()
        if element_id in ELEMENTS:
            absences.append(Absence(element_id, str(entry.get("reason") or "").strip()
                                    or "the model gave no reason"))
        elif element_id:
            rejected.append(f"{element_id}: not an element of the base")

    return Mapping(refs=tuple(refs), absences=tuple(absences), rejected=tuple(rejected))


def _as_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    return []


def propose(
    statements: list[RenderedStatement],
    *,
    column: int,
    agent: Any | None = None,
    model: str = DEFAULT_MODEL,
) -> Mapping:
    """Ask a model which presented line is which element.

    Args:
        statements: parsed rendered statements, primary and notes alike.
        column: which period column the mapping is for. Only lines with a value
            in that column are offered, so an element absent from the period is
            absent from the prompt too.
        agent: an injected callable taking a task string and returning the
            model's text. Tests pass one; production leaves it ``None``.
        model: the model id used when no agent is injected.

    Never raises on a model that answers badly: an unusable answer becomes an
    empty mapping, and every element then falls through to whatever the caller
    does with an unplaced element.
    """
    task = (
        f"{_system()}\n\nThe normalised elements:\n{elements_as_prompt()}\n\n"
        f"The statements as presented:\n{statements_as_prompt(statements, column=column)}\n\n"
        'Answer as JSON: {"mapped": [{"element_id": ..., "statement": ..., "label": ...}], '
        '"absent": [{"element_id": ..., "reason": ...}]}'
    )
    try:
        text = agent(task) if agent is not None else _default_agent(model)(task)
        return parse_mapping(json.loads(_json_block(text)))
    except Exception as exc:  # noqa: BLE001 - a bad answer is an empty mapping, not a crash
        return Mapping(refs=(), absences=(), rejected=(f"the model's answer was unusable: {exc}",))


def _default_agent(model: str):
    """A mapping agent at temperature zero.

    Two runs over the same filing must not disagree about which lines exist. A
    filing is immutable once accepted, so its mapping is a property of the
    document rather than of the day it was read — temperature zero is the
    cheapest part of holding that, and caching the mapping by accession is the
    rest of it, which belongs to whatever stores these bases.

    No system prompt: the instructions travel in the task instead, so an
    injected agent gets exactly what the default one gets. They used to be in
    both places, which sent the whole of them twice per call.
    """
    from lazybridge import Agent, LLMEngine

    agent = Agent(
        engine=LLMEngine(model, temperature=0),
        name="statement_mapper",
    )
    return lambda task: agent(task).text()


def _json_block(text: str) -> str:
    """The JSON object in a model's answer, whatever it wrapped it in."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


__all__ = [
    "DEFAULT_MODEL",
    "Absence",
    "LineRef",
    "Mapping",
    "elements_as_prompt",
    "parse_mapping",
    "propose",
    "statements_as_prompt",
]
