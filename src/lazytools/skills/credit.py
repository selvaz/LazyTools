"""A credit analyst: one agent, an instruction catalogue, and a normalised base.

The shape is the point. The agent's static prompt holds only its identity, how
to read the evidence, and a CATALOGUE of what else it can load — about seven per
cent of the library. Everything specific is pulled in when the question calls
for it, through one tool.

The base arrives as context rather than as something the agent fetches. It has
already been through classification, cross-checks and the state machine, so the
agent's job starts where the data work ends: it reads states, not sources, and
it cannot reach past them to a figure that failed its own check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lazytools.financials.normalised import NormalisedBase
from lazytools.skills.blocks import Library, instruction_tools, load_library

_LIBRARY_DIR = Path(__file__).parent / "library"
#: The common, sector-neutral library that ships with this package.
COMMON_LIBRARY = _LIBRARY_DIR / "credit_analyst.md"
#: Sector libraries that ship with it, by the name a caller passes as ``sector``.
SECTOR_LIBRARIES = {"retail": _LIBRARY_DIR / "sector_retail.md"}


def load_common_library(sector: str | None = None) -> Library:
    """The credit library, with a sector layer on top when one is named.

    Raises:
        KeyError: for a sector with no library. Falling back to the common layer
            alone would answer a sector question with sector-neutral
            instructions and say nothing about having done so.
    """
    if sector is None:
        return load_library(COMMON_LIBRARY)
    if sector not in SECTOR_LIBRARIES:
        raise KeyError(
            f"no library for sector {sector!r}; available: {', '.join(sorted(SECTOR_LIBRARIES))}"
        )
    return load_library(COMMON_LIBRARY, SECTOR_LIBRARIES[sector])


def system_prompt(library: Library, *, sector: str | None = None) -> str:
    """Identity, evidence rules and the catalogue — and nothing else.

    The two ``core`` blocks are inlined because they govern every answer and an
    agent that had to load them could answer before it did. Everything else is
    a line in the catalogue until it is needed.
    """
    core = library.render(["core/identity", "core/evidence"])
    sector_note = (
        f"\n\nYou are assessing an issuer in: {sector}. Load the sector library's "
        "blocks for thresholds and sector metrics; the common blocks below state "
        "no levels, deliberately."
        if sector else
        "\n\nNo sector library is loaded, so you have no thresholds. Say which "
        "level would change your view and that the level is yours."
    )
    return (
        f"{core}{sector_note}\n\n"
        "## Instruction blocks you can load\n\n"
        "Call load_instructions with the ids you need, comma-separated. Load what "
        "the question calls for, not everything: what you load is what you have "
        "read, and a prompt full of instructions nobody applied is worse than a "
        "short one.\n\n"
        f"{library.catalogue()}"
    )


def base_as_context(base: NormalisedBase) -> str:
    """The normalised base, as the agent sees it.

    Every element travels with its state, its route and — when it is blocked —
    the reason. A figure with no state would be a figure the agent could use
    without knowing what it is worth.
    """
    payload = base.to_dict()
    payload["elements"] = {
        key: {k: v for k, v in element.items()
              if k in ("value", "state", "route", "blocked_reason", "meaning")
              and v not in (None, "", [])}
        for key, element in payload["elements"].items()
    }
    return json.dumps(payload, indent=1, ensure_ascii=False)


def credit_analyst(
    *,
    model: str,
    library: Library | None = None,
    sector: str | None = None,
    **agent_kwargs: Any,
) -> Any:
    """Build the analyst agent.

    Args:
        model: the model id to run it on.
        library: an instruction library; the common one by default.
        sector: names the sector library in play, when one is loaded.
        **agent_kwargs: forwarded to ``Agent``.

    Pass the base through :func:`base_as_context` into the envelope's context,
    or simply include it in the task.
    """
    from lazybridge import Agent, LLMEngine

    library = library or load_common_library(sector)
    return Agent(
        engine=LLMEngine(model, system=system_prompt(library, sector=sector)),
        tools=instruction_tools(library),
        name="credit_analyst",
        **agent_kwargs,
    )


__all__ = [
    "COMMON_LIBRARY",
    "SECTOR_LIBRARIES",
    "base_as_context",
    "credit_analyst",
    "load_common_library",
    "system_prompt",
]
