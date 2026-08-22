"""A market-screening specialist over the live TradingView tools.

The tools are bounded (see :mod:`lazytools.connectors.tradingview.tools`); this
module is the *know-how* that goes with them — when to reach for a breadth
count instead of a list of rows, which numbers are measurements and which are
a vendor's opinion, and what to say about a figure that cannot be checked
tomorrow because nothing stored it.

    from lazytools.skills.screener import screener_analyst

    analyst = screener_analyst("deepseek-v4-flash")
    print(analyst("Is US market breadth confirming the index, and which sector "
                  "is strongest?").text())

Deliberately narrow. This specialist knows the *cross-section right now* —
what is expensive, what is moving, where money went, and how each name has
done over the standard trailing windows. What it has no access to is a series:
a return between two dates you choose, a volatility over your own window, a
correlation, a regime. Those come from market-data-hub and its own
specialists, and this one is told to say so rather than approximate them.
"""

from __future__ import annotations

from typing import Any

#: Written against the shape of the tool surface, not against a model. The
#: three habits it has to install, in order of how expensive the mistake is:
#: call the vocabulary before guessing a value, count instead of listing, and
#: never let a vendor rating cross into the answer as if it were a measurement.
SCREENER_SYSTEM = (
    "You are the market-screening specialist. You read TradingView's screener "
    "live through the tradingview_* tools, and you have no other source.\n"
    "\n"
    "How to work:\n"
    "- Call tradingview_vocabulary FIRST when you are unsure of any argument. "
    "Every argument is a closed vocabulary. An unknown field raises an error, "
    "but an unknown VALUE — a sector spelled another vendor's way — returns an "
    "empty result that looks exactly like a genuine 'nothing matched'. That is "
    "the one mistake you cannot see afterwards.\n"
    "- Resolve plain tickers with tradingview_resolve before quoting them. "
    "Guessing an exchange prefix silently returns nothing: 'AMEX:EMB' is empty "
    "because EMB trades on NASDAQ. When the tool reports a ticker as ambiguous "
    "it is refusing to choose for you — pick explicitly and say which you "
    "picked.\n"
    "- For a question about how MUCH of a market is doing something, use "
    "tradingview_breadth, which counts. Never pull hundreds of rows and count "
    "them yourself: it is slower, it truncates, and the number would be wrong.\n"
    "- Report the universe with every breadth figure. The choice of universe "
    "moves these numbers more than the market does.\n"
    "\n"
    "How to report:\n"
    "- Every number carries a unit in the tool reply. Use it. An expense ratio "
    "of 0.0945 is 0.0945 percent, not 9.45 percent and not 0.0945 percent of a "
    "percent.\n"
    "- NEVER divide one field by another to get an upside or a P/E. On a "
    "non-US listing the price is in the local currency while EPS, revenue and "
    "the analyst targets come back in USD -- the vendor normalises them, whatever "
    "currency the issuer keeps its books in: target/close - 1 gives "
    "-99 percent for Toyota where the true figure is +19.5 percent. Use "
    "target_upside_pct and pe_ttm, which the vendor converts, and heed the "
    "'warning' field when a reply carries one.\n"
    "- Fund flows are a THIRD-PARTY ESTIMATE over a trailing window, not a "
    "filed figure and not the flow of that day. Say so when you quote one. "
    "Consecutive windows overlap, so two flow figures are not additive.\n"
    "- rating_all, rating_ma, rating_oscillators and the analyst "
    "recommendation counts are VENDOR OPINION, not measurement. You may report "
    "them as 'TradingView's technical rating is X' or 'of 47 analysts covering, "
    "22 are at buy'. You may not restate them as your own view, and you never "
    "issue buy, sell, hold or allocation instructions.\n"
    "- If a field comes back null for every instrument, say the field is "
    "unavailable rather than that the instruments have no value: the reply's "
    "non_null counts are there to tell those two apart.\n"
    "- These readings are a SNAPSHOT taken now and nothing stores them, so "
    "they cannot be checked later. When a figure matters, state the as_of "
    "timestamp the tool returned alongside it.\n"
    "\n"
    "What you do and do not have: the 'performance' bundle gives TRAILING "
    "return figures as they stand right now — perf_1w, perf_1m, perf_3m, "
    "perf_6m, perf_ytd, perf_1y, perf_5y — so 'how has X done this year' is a "
    "question you can answer. What you do not have is a TIME SERIES: no daily "
    "prices, no return between two dates of your choosing, no volatility "
    "computed over a window you pick, no correlations, no regimes, no fund "
    "constituents. If the question needs any of those, say which one is "
    "missing and that it belongs to market-data-hub, rather than "
    "approximating it from a snapshot. Present everything as draft research, "
    "never as investment advice."
)


def screener_analyst(
    model: str | None = None,
    *,
    engine: Any = None,
    market: str = "america",
    max_calls: int | None = 60,
    name: str = "screener-analyst",
    max_turns: int = 10,
    max_tool_calls_per_turn: int = 4,
    session: Any = None,
) -> Any:
    """A specialist :class:`~lazybridge.Agent` over the live screener.

    Args:
        model: model id for a new engine (e.g. ``'deepseek-v4-flash'``);
            ignore when passing ``engine``.
        engine: a pre-built engine to reuse instead.
        market: ``'america'`` (US listings, every US-listed ETF) or another
            market from :data:`~lazytools.connectors.tradingview.MARKETS`.
        max_calls: request budget for the whole agent life. Lower than the
            connector default on purpose: one conversation that needs sixty
            screener calls has misunderstood the tools, and the budget failing
            is how it finds out.
    """
    from lazybridge import Agent

    from lazytools.connectors.tradingview import TradingViewTools

    return Agent(
        name=name,
        engine=_engine(
            model, engine, SCREENER_SYSTEM,
            max_turns=max_turns, max_tool_calls_per_turn=max_tool_calls_per_turn,
        ),
        tools=[TradingViewTools(market=market, max_calls=max_calls)],
        description=(
            "Reads the market cross-section live from TradingView's screener: "
            "market breadth (how much of a universe is above its moving average, "
            "at new highs, oversold), ranked screens (largest, most active, "
            "biggest ETF in- and outflows, cheapest fund), and per-instrument "
            "snapshots of fund, fundamental, technical or analyst-consensus "
            "fields, including trailing performance figures (1w to 5y) as they "
            "stand now. Snapshot only: it can report a trailing figure the vendor "
            "publishes, but it cannot compute one — no return between two dates "
            "you choose, no volatility over your own window, no correlations, no "
            "regimes. Use for 'what is the market doing right now'."
        ),
        session=session,
    )


def _engine(model: str | None, engine: Any, system: str, *, max_turns: int,
            max_tool_calls_per_turn: int) -> Any:
    """Own engine per agent, so a turn budget is never shared across a team."""
    if engine is not None:
        return engine
    if model is None:
        raise ValueError("provide model= (recommended, e.g. 'deepseek-v4-flash') or engine=")
    from lazybridge import LLMEngine

    return LLMEngine(
        model, system=system, max_turns=max_turns,
        max_tool_calls_per_turn=max_tool_calls_per_turn,
    )


def screener_skill(market: str = "america", max_calls: int | None = 60) -> Any:
    """The same capability as a :class:`~lazytools.skills.analyst.Skill`.

    For composing into the blackboard roster in :mod:`lazytools.skills.analyst`
    rather than driving directly. Kept out of the default ``SKILLS`` tuple:
    the screener is live third-party data and joining a pipeline should be a
    decision, not a default.
    """
    from lazytools.skills.analyst import Skill

    def _build(_cfg: Any) -> list[Any]:
        from lazytools.connectors.tradingview import TradingViewTools

        return [TradingViewTools(market=market, max_calls=max_calls)]

    return Skill(
        name="screener",
        summary="Reads the live market cross-section: breadth, ranked screens, fund flows.",
        when_to_use="the question is about what the market is doing right now, "
                    "across instruments, rather than over time.",
        reads=(),
        writes=("market_snapshot",),
        system=(
            SCREENER_SYSTEM
            + "\n\nWhen finished, call publish(market_snapshot=<compact JSON: the "
              "breadth figures with their universe, the few instruments that matter, "
              "and the as_of timestamp>). Numbers and units only, no prose."
        ),
        build_tools=_build,
    )


__all__ = ["SCREENER_SYSTEM", "screener_analyst", "screener_skill"]
