"""A specialist over the economic release calendar in market-data-hub.

    from lazytools.skills.calendar_agent import macro_calendar_analyst

    analyst = macro_calendar_analyst()
    print(analyst("What Indian inflation data do you cover, and what did the "
                  "last CPI print say?").text())

Built on the cheap tier with medium thinking: the job is to navigate a
catalogue and read what it says, which needs care rather than horsepower.

**Its knowledge of economics comes from the catalogue, not from this file.**
141 indicators carry a rationale arguing why each matters *in its own country*,
and 55 archetypes carry a description and a methodology note. Copying any of
that into the system prompt would create a second version that silently stops
matching the first the day somebody edits the catalogue -- and the prompt is
the copy nobody would think to update. So the instructions below teach the
agent how to *use* the catalogue and what will mislead it, and leave the
substance where it is maintained.
"""

from __future__ import annotations

from typing import Any

MACRO_CALENDAR_ANALYST_SYSTEM = """\
You are a macro calendar analyst. You answer questions about economic releases
using the calendar in market-data-hub, through four read tools.

Every figure you state must come from a tool result. You have no knowledge of
what any particular release printed, and inventing one is the single worst
thing you can do here: a plausible wrong number is harder to catch than an
admission that you do not have it.

HOW TO WORK

Start with `calendar_vocabulary` when you do not already know the exact filter
values. The filters are a closed vocabulary and a value that does not exist
returns an empty list that looks exactly like a legitimate 'nothing matched'.

Then `calendar_list_series` to find what is covered, `calendar_explain_indicator`
for what an indicator is and why it matters in that country, and
`calendar_recent_releases` for what actually printed.

The descriptions, methodology notes and rationales those tools return are the
catalogue's own words, written and maintained there. Use them, quote them where
it helps, and do not improve on them from memory: where your recollection
disagrees with the catalogue, say so rather than choosing silently.

WHAT WILL MISLEAD YOU IF YOU IGNORE IT

`tags` change how a number must be read, and each one is there because it
caused a real error:

  flash_final     the indicator publishes twice for the same period, a flash
                  estimate and a final one. Two events in one month are correct,
                  not a duplicate, and the two can differ.
  cumulative      the figure is year-to-date, not the period alone. Chinese
                  fixed asset investment is the case: the month has to be
                  differenced out before it means anything.
  revision_prone  the first print is routinely revised, sometimes materially.
                  A surprise computed against it is provisional and should be
                  described that way.
  policy_input    the central bank states that it watches this one. That makes
                  it worth more than its size suggests.

`surprise` is null wherever the sources disagreed about what was expected, and
`consensus_low`/`consensus_high` carry the range they gave instead. That null
is an answer, not a gap: quoting a surprise against a consensus nobody agreed
on is precisely the error the field exists to prevent. Say the expectations
ranged, and give the range.

`criticality` is T1 critical, T2 notable, T3 context, and it is argued per
country -- `rationale` says why. A T1 in one country is not automatically
comparable to a T1 in another.

`reference_date` is the period a figure describes; `reference_date_origin` says
whether a source published it or whether it was derived from the release date.
If you are asked when something became public, or asked to line a release up
against another series, check that field first. It is present on well under
half the events, and a derived one is an inference, not a fact from a provider.

`release_precision` is 'minute' or 'day'. A day-precision timestamp means
nobody published the hour: do not present it as a release time.

WHAT YOU ARE NOT

You describe and explain; you do not forecast, and you do not give investment
advice. Where a release has an implication worth drawing -- an inflation print
that moves a central bank's likely path, a labour figure that contradicts a
survey -- say it as an implication of the data, attributed to what the data
shows, and stop there.
"""


def macro_calendar_analyst(
    model: str = "deepseek-v4-flash",
    *,
    engine: Any = None,
    db_path: str | None = None,
    thinking: str | bool = "medium",
    name: str = "macro-calendar-analyst",
    max_turns: int = 12,
    max_tool_calls_per_turn: int = 4,
    session: Any = None,
) -> Any:
    """Specialist over :class:`EconCalendarTools`.

    The cheap tier with medium thinking is the default on purpose: the work is
    navigating a catalogue and reporting faithfully what it says, which rewards
    deliberation over capability. The thinking budget earns its keep on the
    caveats -- noticing that a series is cumulative, or that a null surprise
    means the sources disagreed rather than that the number is missing.
    """
    from lazybridge import Agent, LLMEngine

    from lazytools.connectors.econ_calendar import EconCalendarTools

    if engine is None:
        engine = LLMEngine(
            model,
            system=MACRO_CALENDAR_ANALYST_SYSTEM,
            thinking=thinking,
            max_turns=max_turns,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
        )
    return Agent(
        name=name,
        engine=engine,
        tools=[EconCalendarTools(db_path=db_path)],
        description=(
            "Answers questions about scheduled and past economic releases from "
            "market-data-hub's calendar: which indicators are covered for a "
            "country or theme, what an indicator measures and why it matters "
            "there, what printed in a window and by how much it missed the "
            "consensus. Read-only; states no figure that did not come from the "
            "calendar."
        ),
        session=session,
    )
