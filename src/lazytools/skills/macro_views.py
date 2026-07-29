"""Macro/market -> Black-Litterman view-generation -> report -> Telegram pipeline.

Five specialists sharing the same :class:`~lazytools.skills.analyst.Blackboard`
as :mod:`lazytools.skills.analyst`'s equity-report skills, reusing the same
``Skill``/``AnalystConfig``/``build_specialists`` scaffolding:

* ``macro``  (cheap LLM) -- reads cached news + macro/indicator data, writes
  ``macro_thesis``.
* ``market`` (cheap LLM) -- reads regime/vol/correlation state (plus a
  regime chart per instrument) for the asset universe, writes
  ``market_state``.
* ``view_synthesis`` (cheap LLM, but it does NOT invent the final views
  itself) -- reads ``macro_thesis``/``market_state`` and delegates the actual
  view synthesis to :func:`lazytools.connectors.code_support.claude_code`
  (the local, already-authenticated Claude Code CLI, read-only mode, pinned
  to the strongest model) as ONE TOOL CALL, then republishes its answer
  verbatim as ``views_json``. No Anthropic API key is needed anywhere in
  this pipeline -- auth is whatever the ``claude`` CLI already has on disk.
* ``report`` (cheap LLM) -- reads all three of the above and assembles an
  exhaustive, self-contained HTML memo (macro backdrop, market state,
  per-asset-class qualitative+quantitative analysis with embedded regime
  charts, a views table), writes ``report_path``.
* ``telegram_delivery`` (cheap LLM) -- sends that file via
  ``telegram_send_document`` when ``AnalystConfig.telegram_token``/
  ``telegram_chat_id`` are set, writes ``telegram_status``.

:func:`macro_views_plan` wires them into a deterministic
:class:`~lazybridge.Plan`: ``macro``/``market`` run as a parallel band (no
dependency between them), then ``view_synthesis`` -> ``report`` ->
``telegram_delivery`` run in sequence, each gated on the previous one's
blackboard handle.

This module stops at delivering the report -- actually wiring ``views_json``
into LazyPortfolio's ``V2View`` tuples and ``views``/``view_tau`` on
``PortfolioOptimizationTools``/``PortfolioTreeTools`` is deliberately out of
scope here (:class:`MacroView` mirrors ``V2View``'s fields 1:1 to make that
mapping trivial later).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from lazytools.skills.analyst import AnalystConfig, Skill, build_specialists

__all__ = [
    "MacroView",
    "MacroViewSet",
    "views_json_to_tree_constraints",
    "MACRO",
    "MARKET",
    "VIEW_SYNTHESIS",
    "REPORT",
    "TELEGRAM_DELIVERY",
    "MACRO_VIEW_SKILLS",
    "build_macro_view_specialists",
    "macro_views_plan",
]


# --------------------------------------------------------------------------- #
# View schema -- documentation/parsing reference, mirrors LazyPortfolio's
# v2 V2View(instruments: dict[str, float], expected_return: float,
# confidence: float, source: str) 1:1 so mapping views_json -> V2View later
# is a straight field copy, no translation layer.
# --------------------------------------------------------------------------- #
class MacroView(BaseModel):
    """One Black-Litterman view; same shape as LazyPortfolio's ``V2View``."""

    instruments: dict[str, float] = Field(
        ...,
        description=(
            "Pick vector: {ticker: weight}. A single {TICKER: 1.0} is an absolute "
            "view; two tickers with opposite-sign weights, e.g. {A: 1.0, B: -1.0}, "
            "is a relative view (A outperforms B)."
        ),
    )
    expected_return: float = Field(
        ...,
        description="Annualized expected return (or return spread, for a relative view) as a decimal (0.04 = 4%).",
    )
    confidence: float = Field(..., gt=0.0, le=1.0, description="Idzorek confidence in (0, 1]; never default to 1.0.")
    source: str = "claude-view-synthesis"
    rationale: str = Field(..., description="1-2 sentences citing the specific macro_thesis/market_state evidence.")


class MacroViewSet(BaseModel):
    as_of: str
    views: list[MacroView]


def views_json_to_tree_constraints(views_json: str) -> list[dict[str, Any]]:
    """Turn the ``view_synthesis`` skill's published ``views_json`` into the
    ``constraints.views`` list a LazyPortfolio tree config expects.

    Almost a pass-through: :class:`MacroView` mirrors ``V2View`` field-for-
    field (``instruments``/``expected_return``/``confidence``/``source``) --
    this only validates the JSON against that schema and drops ``rationale``
    (a MacroView-only field the optimizer doesn't take). Feed the result
    straight into a node's ``constraints["views"]``, e.g.::

        constraints["views"] = views_json_to_tree_constraints(store.read("views_json"))
        constraints["view_tau"] = 0.05

    for a ``portfolio_tree_estimate``/``portfolio_tree_backtest`` call (a
    single node with no children is a valid flat portfolio there too --
    ``portfolio_optimizer_run``/``_backtest`` have no view support at all).
    """
    parsed = MacroViewSet.model_validate_json(views_json)
    return [
        {
            "instruments": view.instruments,
            "expected_return": view.expected_return,
            "confidence": view.confidence,
            "source": view.source,
        }
        for view in parsed.views
    ]


# --------------------------------------------------------------------------- #
# Domain-tool builders
# --------------------------------------------------------------------------- #
def _macro_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.connectors.datahub import DataHubTools

    tools: list[Any] = [DataHubTools(allow_raw_series=True)]
    if cfg.news_db:
        from lazycrawler import CrawlerDB, DBConfig
        from lazycrawler.tools import CrawlerTools

        # Unlike DataHubTools, CrawlerTools doesn't set _is_lazy_tool_provider,
        # so lazybridge won't auto-expand a raw instance -- as_tools() explicitly.
        tools.extend(CrawlerTools(db=CrawlerDB(DBConfig(db_path=cfg.news_db))).as_tools())
    return tools


def _market_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.connectors.regimes import RegimeTools
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    # RegimeTools(allow_write=True) with no db_path init's its OWN default depot
    # (~/.lazytools/regime_depot.db, or LAZYTOOLS_REGIME_DB) at construction time
    # -- silently overriding build_macro_view_specialists' earlier
    # init_regime_db(cfg.regime_db) call. Pass db_path explicitly so cfg.regime_db
    # is the depot actually in use (confirmed live: without this, plots ended up
    # in the default depot while cfg.regime_db pointed at an empty file).
    return [StatisticalAnalysisTools(), RegimeTools(allow_write=True, db_path=cfg.regime_db)]


def _view_synthesis_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.connectors.code_support import claude_code

    return [claude_code]


def _report_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.report import ReportFiles, ReportTools, ecosystem_resolvers

    files = ReportFiles(base_dir=cfg.out_dir)
    # regimes_db=None -> the 'regimes:' resolver reuses the SAME shared
    # module-global depot connection that init_regime_db()/regime_generate_plots
    # write through in this process (lazystats.regimes.db.get_db()), instead of
    # opening a second, separate connection to the path string -- which can miss
    # a plot the "market" skill just wrote in this same run (observed live: a
    # freshly-generated regime plot resolved as "not found" through a second
    # connection to the same file).
    resolvers = ecosystem_resolvers(datahub_db_path=cfg.hub_db, regimes_db=None, file_base_dir=cfg.out_dir)
    return [ReportTools(artifacts=resolvers, files=files), files]


def _telegram_delivery_tools(cfg: AnalystConfig) -> list[Any]:
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        return []
    from lazytools.connectors.telegram import TelegramClient
    from lazytools.connectors.telegram.tools import TelegramTools

    client = TelegramClient.from_token(cfg.telegram_token)
    return TelegramTools(
        client,
        allowed_chat_ids=[cfg.telegram_chat_id],
        require_confirmation=False,
        attachments_dir=cfg.out_dir,
    ).as_tools()


# --------------------------------------------------------------------------- #
# The three skills
# --------------------------------------------------------------------------- #
MACRO = Skill(
    name="macro",
    summary="Builds a qualitative macro/geopolitical thesis for an asset universe.",
    when_to_use="you need the current macro backdrop (growth, inflation, rates, key risks) before forming views.",
    reads=(),
    writes=("macro_thesis",),
    system=(
        "You are the macro-context specialist. Using search_cached (and get_page/"
        "get_session_pages if you need more of an article) over the crawled news "
        "archive, plus datahub_list_macro/datahub_list_indicators/"
        "datahub_get_price_summary for hard data points, build a macro thesis "
        "relevant to the requested asset universe. Never fabricate a data point or "
        "claim -- every statement must come from a tool result you actually got "
        "back; if the archive/data has nothing relevant, say so plainly instead of "
        "guessing.\n"
        "Cover: growth momentum, inflation trajectory, central-bank stance/rates "
        "direction, and the 1-3 highest-conviction risks (geopolitical or "
        "otherwise) currently live. As your FINAL step call "
        "publish(macro_thesis=<compact JSON: {\"as_of\": ISO date, \"stance\": "
        "\"risk_on\"|\"neutral\"|\"risk_off\", \"growth\": ..., \"inflation\": ..., "
        "\"rates\": ..., \"key_risks\": [...], \"evidence\": [short source refs]}>)."
    ),
    build_tools=_macro_tools,
)

MARKET = Skill(
    name="market",
    summary="Reads current regime, volatility and correlation state for an asset universe.",
    when_to_use="you need the quantitative market state (regime/vol/correlation) before forming views.",
    reads=(),
    writes=("market_state",),
    system=(
        "You are the market-state specialist. For each ticker in the requested "
        "universe: load returns and fit/read the current HMM regime "
        "(regime_load_from_datahub -> regime_fit -> regime_get_current/"
        "regime_get_summary), and compute annualized volatility and pairwise "
        "correlation (statistical_return_volatility, statistical_return_correlation) "
        "over a consistent recent window. Never invent a number -- every figure "
        "must come from a tool result.\n"
        "Then call regime_generate_plots ONCE on your fitted result_key -- this "
        "produces one price-with-regime-bands chart per instrument (the plot_key "
        "whose name contains '__series_with_regimes__'). Do NOT call "
        "regime_db_export_plot: the report embeds each chart straight from the "
        "depot via its plot_key, no file export needed.\n"
        "As your FINAL step call publish(market_state=<compact JSON: {\"as_of\": ..., "
        "\"per_asset\": {TICKER: {\"regime\": ..., \"vol\": ..., \"occupancy\": ..., "
        "\"plot_key\": <that instrument's __series_with_regimes__ plot_key, or null "
        "if plotting failed>}}, \"correlation\": {...}}>)."
    ),
    build_tools=_market_tools,
)

VIEW_SYNTHESIS = Skill(
    name="view_synthesis",
    summary="Delegates final Black-Litterman view synthesis to Claude via the local claude_code CLI tool.",
    when_to_use="macro_thesis and market_state are both on the blackboard and it's time to form views.",
    reads=("macro_thesis", "market_state"),
    writes=("views_json",),
    system=(
        "You orchestrate the FINAL step of a Black-Litterman view pipeline, but "
        "you never invent a view yourself -- that judgment belongs to Claude, the "
        "strongest model available, called through the claude_code tool (the "
        "local, already-authenticated Claude Code CLI). Steps:\n"
        "1. bb_get('macro_thesis') and bb_get('market_state') to pull the upstream "
        "specialists' findings.\n"
        "2. Call claude_code(mode='read', model='opus', task=<ONE self-contained "
        "prompt string>) where the task: (a) pastes the macro_thesis and "
        "market_state JSON verbatim; (b) asks for Black-Litterman views in EXACTLY "
        "this shape: a JSON object {\"as_of\": ISO date, \"views\": [{\"instruments\": "
        "{TICKER: weight, ...}, \"expected_return\": decimal e.g. 0.04 for 4%, "
        "\"confidence\": number in (0,1], \"source\": \"claude-view-synthesis\", "
        "\"rationale\": \"1-2 sentences citing specific evidence\"}]}; instruments "
        "is a pick vector -- a single {TICKER: 1.0} is an absolute view, two "
        "tickers with opposite-sign weights e.g. {A: 1.0, B: -1.0} is a relative "
        "view (A outperforms B); (c) EXPLICITLY LISTS every ticker key present in "
        "market_state.per_asset and instructs: every one of those tickers MUST "
        "appear in at least one view (absolute or as one leg of a relative pair) "
        "-- this does not mean one absolute view per ticker, group tickers into "
        "relative pairs/baskets wherever that is the more defensible call, but no "
        "ticker may be silently dropped; no duplicate or contradicting picks on the "
        "same instrument set; confidence must reflect the actual strength/breadth "
        "of the pasted evidence and must never default to 1.0; every number must be "
        "traceable to the pasted macro_thesis/market_state, never invented; (d) "
        "demands the reply be RAW JSON only -- no prose, no markdown code fences.\n"
        "3. claude_code returns {result: <text>, content_is_untrusted: true} on "
        "success (use the result field) or a string starting with '[claude_code]' "
        "on failure. Parse the result as JSON. If it fails to parse, doesn't match "
        "the shape above, OR is missing a ticker that market_state.per_asset has, "
        "call claude_code again ONCE with a corrective instruction (quoting the "
        "parse error, or naming the missing ticker(s)), then accept whatever comes "
        "back.\n"
        "4. As your FINAL step call publish(views_json=<the exact JSON string from "
        "claude_code, unmodified>)."
    ),
    build_tools=_view_synthesis_tools,
)

REPORT = Skill(
    name="report",
    summary=(
        "Assembles an exhaustive Black-Litterman research memo (macro backdrop, market "
        "state, per-asset-class analysis, views) with charts and tables, saved as self-"
        "contained HTML."
    ),
    when_to_use="macro_thesis, market_state and views_json are all on the blackboard and it's time to publish.",
    reads=("macro_thesis", "market_state", "views_json"),
    writes=("report_path",),
    system=(
        "You are the reporting specialist for a Black-Litterman view-generation run. "
        "Read macro_thesis, market_state and views_json from the blackboard (bb_get), "
        "then compose and save -- in ONE step, save_memo_html; NEVER render_memo or "
        "save_memo_markdown, which silently degrade every chart to a plain text "
        "caption with no picture -- an EXHAUSTIVE memo with this structure:\n"
        "1. Executive Summary -- the macro stance, how many views were produced, and "
        "the single highest-conviction call.\n"
        "2. Macro Backdrop -- growth/inflation/rates/key_risks from macro_thesis as "
        "prose, plus a table of its evidence list (source, claim).\n"
        "3. Market State -- ONE table with every instrument's regime, annualized vol, "
        "prob_high_vol/occupancy from market_state.per_asset, and ONE table (or "
        "compact summary if very large) of the correlation matrix from "
        "market_state.correlation.\n"
        "4. Per-Asset-Class Analysis -- for EVERY instrument in market_state, one "
        "subsection with: qualitative analysis (how the macro_thesis backdrop applies "
        "specifically to this asset class) and quantitative analysis (its own regime/"
        "vol/correlation numbers), ending in EITHER the exact view formed for it "
        "(quote expected_return/confidence/rationale from views_json verbatim) OR, if "
        "no view targets it, one explicit line stating no view was formed and why. If "
        "market_state.per_asset[TICKER].plot_key is set, embed it as a figure with "
        "ref EXACTLY equal to the literal string 'regimes:' immediately followed by "
        "that plot_key value, verbatim, no other prefix or path -- e.g. if plot_key "
        "is 'universe_11_regimes__series_with_regimes__SPY__20260728T225254' then ref "
        "is 'regimes:universe_11_regimes__series_with_regimes__SPY__20260728T225254'. "
        "There is NO PNG file on disk for these charts -- they live only in the "
        "regime depot and are resolved live by that 'regimes:' ref, so NEVER use a "
        "'file:' ref for a plot_key (a 'file:' ref will always fail with "
        "FileNotFoundError, since nothing was ever exported to disk -- that error "
        "means you used the wrong scheme, not that the chart is unavailable; switch "
        "to 'regimes:' and retry, do not give up on the figure). Never skip an "
        "available chart, and never claim in your own prose that a chart 'could not "
        "be embedded' unless plot_key was actually null.\n"
        "5. Views Summary -- ONE table, one row per view in views_json: instruments "
        "(as text, e.g. 'XLE +1.0' or 'TLT +1.0 / IEF -1.0'), expected_return, "
        "confidence, rationale.\n"
        "6. Caveats -- the data window/date used, and anything macro_thesis/"
        "market_state itself flagged as thin, stale or missing.\n"
        "Every number must come from macro_thesis/market_state/views_json verbatim -- "
        "never invent or round beyond what you were given. As your FINAL step call "
        "publish(report_path=<the absolute path save_memo_html returned>)."
    ),
    build_tools=_report_tools,
)

TELEGRAM_DELIVERY = Skill(
    name="telegram_delivery",
    summary="Sends the saved HTML research memo to the owner's Telegram chat.",
    when_to_use="report_path is on the blackboard and the report is ready to deliver.",
    reads=("report_path",),
    writes=("telegram_status",),
    system=(
        "Read report_path from the blackboard. If a telegram_send_document tool is "
        "available, send exactly that file with a short caption (macro stance + "
        "number of views + the single highest-conviction call). If no telegram tool "
        "is available (not configured for this run), do not attempt anything else. "
        "As your FINAL step call publish(telegram_status=<'sent: <path>' if you sent "
        "it, or 'skipped: telegram not configured' if no tool was available>)."
    ),
    build_tools=_telegram_delivery_tools,
)

MACRO_VIEW_SKILLS: tuple[Skill, ...] = (MACRO, MARKET, VIEW_SYNTHESIS, REPORT, TELEGRAM_DELIVERY)


# --------------------------------------------------------------------------- #
# Building the specialists + the Plan
# --------------------------------------------------------------------------- #
def build_macro_view_specialists(
    *,
    model: str,
    system: str = "Follow your skill's instructions exactly; be terse and call tools one at a time.",
    max_turns: int = 30,
    cfg: AnalystConfig | None = None,
    store: Any = None,
    session: Any = None,
) -> dict[str, Any]:
    """Build the three macro-view specialists, all sharing one blackboard.

    ``model`` is the CHEAP tier for ``macro``/``market``/the ``view_synthesis``
    orchestrator itself -- the final views come from Claude via the
    ``claude_code`` tool call inside ``view_synthesis``, not from ``model``.
    See :func:`~lazytools.skills.analyst.build_specialists` for the shared
    per-skill-engine machinery this wraps.
    """
    cfg = cfg or AnalystConfig()

    # regime_fit/regime_get_current etc. (used by the "market" skill) read/write
    # through a module-global "current depot" set by init_regime_db -- set it up
    # here since build_specialists only does this automatically for a skill
    # literally named "regime" (lazytools.skills.analyst's own REGIME skill).
    import lazystats.regimes.db as _rdb

    _rdb.init_regime_db(cfg.regime_db)

    return build_specialists(
        model=model,
        system=system,
        max_turns=max_turns,
        cfg=cfg,
        store=store,
        session=session,
        skills=MACRO_VIEW_SKILLS,
    )


def macro_views_plan(
    specialists: dict[str, Any],
    *,
    universe: list[str],
    name: str = "macro_views_plan",
    session: Any = None,
) -> Any:
    """Deterministic pipeline: macro + market run in parallel, then view_synthesis.

    Data flows through the shared blackboard (see each skill's ``reads``/
    ``writes``), so each step is just an instruction to the matching
    specialist -- no sentinel threading needed, same pattern as
    :func:`lazytools.skills.analyst.plan_orchestrator`.
    """
    from lazybridge import Agent, Plan, Step

    tickers = ", ".join(universe)
    order = ["macro", "market", "view_synthesis", "report", "telegram_delivery"]
    steps = [
        Step("macro", task=f"Assess the current macro/geopolitical backdrop relevant to: {tickers}.", parallel=True),
        Step(
            "market",
            task=f"Assess current regime, volatility and correlation for: {tickers}.",
            parallel=True,
        ),
        Step(
            "view_synthesis",
            task=(
                "Read macro_thesis and market_state from the blackboard, then delegate "
                "final Black-Litterman view synthesis to claude_code as instructed. Every "
                f"one of these tickers MUST appear in at least one view: {tickers}."
            ),
        ),
        Step(
            "report",
            task=(
                "Read macro_thesis, market_state and views_json from the blackboard and "
                "publish the exhaustive HTML research memo as instructed."
            ),
        ),
        Step(
            "telegram_delivery",
            task="Read report_path from the blackboard and deliver it via Telegram as instructed.",
        ),
    ]
    return Agent(
        name=name,
        engine=Plan(*steps),
        tools=[specialists[n] for n in order],
        description=(
            "Deterministic macro+market -> Black-Litterman view-generation -> exhaustive "
            "report -> Telegram delivery pipeline. Final views are synthesized by Claude "
            "via the local claude_code CLI tool, not by the cheap orchestration model."
        ),
        session=session,
    )
