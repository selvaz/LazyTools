"""The retriever's instruction library, and what reads from it.

The credit analyst got a block library first, and the retriever was left as
hard-coded Python. That was the wrong half to leave out. The retriever is the
side where a wrong judgement produces a figure that looks right — which source
counts, which schedule is the one you want, when a filing's silence means the
issuer has none of something — and those are exactly the judgements that belong
in text a person can read and argue with, not in a string literal inside a
prompt builder.

The line the library does not cross is the one that makes the whole design
work: instructions govern WHERE to look, WHICH source counts and WHEN to stop.
Code still reads every figure. A model that never emits a number cannot invent
one, and no amount of instruction changes that guarantee in either direction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lazytools.skills.blocks import Library, instruction_tools, load_library

_LIBRARY_DIR = Path(__file__).parent / "library"
#: The retrieval library that ships with this package.
RETRIEVER_LIBRARY = _LIBRARY_DIR / "retriever.md"

#: Blocks that govern a mapping call, in the order they are rendered.
#:
#: The mapping call is one turn with no tools, so it cannot fetch anything: what
#: it needs has to be in front of it. These are the blocks that decide whether a
#: line is the right line, which is the whole of that turn's job. The routing
#: and multi-period blocks are deliberately absent — by the time a mapping runs,
#: the source and the period are settled.
MAPPING_BLOCKS = ("core/identity", "source/sec/statements", "source/sec/traps")


def load_retriever_library() -> Library:
    """The retrieval library, validated as it composes."""
    return load_library(RETRIEVER_LIBRARY)


def mapping_instructions(library: Library | None = None) -> str:
    """The instructions in front of a mapping call.

    Rendered from the library rather than written here, so the traps that govern
    a mapping are the same text a reader can find, cite and correct. Every one
    of them was measured against a live filing; a paraphrase in a string literal
    drifts from the measurement and nobody notices.
    """
    library = library or load_retriever_library()
    return library.render(list(MAPPING_BLOCKS))


def system_prompt(library: Library | None = None) -> str:
    """Identity, provenance and the catalogue — for an agentic retriever.

    Not used by the mapping call, which is a single turn. This is the prompt for
    a retriever that has to choose a source before it can map anything, and it
    carries the same shape as the analyst's: the two governing blocks inlined,
    everything else a line in the catalogue until it is needed.
    """
    library = library or load_retriever_library()
    core = library.render(["core/identity", "core/provenance"])
    return (
        f"{core}\n\n"
        "## Instruction blocks you can load\n\n"
        "Call load_instructions with the ids you need, comma-separated. Start "
        "with route/jurisdiction: which source applies decides what can be "
        "answered at all, and loading a source block before settling the route "
        "is reading instructions for a filing you may not be looking at.\n\n"
        f"{library.catalogue()}"
    )


def retriever_agent(*, model: str, library: Library | None = None, **agent_kwargs: Any) -> Any:
    """Build a retriever agent over the instruction library.

    It selects sources and names lines. It does not read figures, and it is
    given no tool that would let it.
    """
    from lazybridge import Agent, LLMEngine

    library = library or load_retriever_library()
    return Agent(
        engine=LLMEngine(model, system=system_prompt(library)),
        tools=instruction_tools(library),
        name="statement_retriever",
        **agent_kwargs,
    )


__all__ = [
    "MAPPING_BLOCKS",
    "RETRIEVER_LIBRARY",
    "load_retriever_library",
    "mapping_instructions",
    "retriever_agent",
    "system_prompt",
]
