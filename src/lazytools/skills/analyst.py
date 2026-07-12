"""Composable analyst *skills*: specialist agents that share a blackboard.

This module turns the LazyTools connector tools into a small set of **skills**.
A skill is a specialist :class:`~lazybridge.Agent` — domain tools plus a
tailored system prompt — whose *description is a contract*: what it does, when
to use it, and which short **handles** it reads from and writes to a shared
blackboard. Because an agent's ``name`` + ``description`` become the tool an
orchestrator sees, an orchestrator built from these skills *knows what each
skill does* without any hand-written recipe.

Design rules that make this composable:

* **Handles, not data, cross the blackboard.** Specialists persist heavy
  artifacts where they belong (prices/facts in the market-data-hub DuckDB,
  fitted regimes and plots in the LazyStats depot, the rendered report on
  disk) and write only short *handles* (a ``result_key``, a ``plot_key``, a
  file path, a compact JSON summary) to a shared :class:`~lazybridge.Store`.
  Downstream skills read those handles back. This is the same discipline that
  keeps big payloads out of the LLM's token stream.
* **One set of skills, three orchestrators.** :func:`plan_orchestrator`
  (deterministic), :func:`blackboard_orchestrator` (flat to-do list) and
  :func:`replan_orchestrator` (adaptive re-planning) all drive the *same*
  specialists over the *same* blackboard — only the top-level strategy changes.

See ``examples/`` for a runnable end-to-end (a quantitative equity report).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# lazybridge is a hard dependency of lazytoolkit; import lazily inside the
# builders so importing this module stays cheap and side-effect free.


# --------------------------------------------------------------------------- #
# Shared blackboard
# --------------------------------------------------------------------------- #
class Blackboard:
    """A shared key/value blackboard exposed as tools, backed by a Store.

    Every specialist gets these tools and the *same* underlying store, so one
    skill can leave a handle (``regime_plot_key``, ``balance_sheet``, …) for
    another to pick up. Values must be short — a key, a path, a number, a
    compact JSON string — never bulk data.
    """

    _is_lazy_tool_provider = True

    def __init__(self, store: Any) -> None:
        self.store = store

    def as_tools(self, *, readable: set[str] | None = None, writable: set[str] | None = None) -> list[Any]:
        """Blackboard tools, optionally SCOPED to a skill's contract.

        ``writable`` / ``readable`` restrict which handles this specialist may
        write / read (its declared ``writes`` / ``reads``). Enforcing the
        contract at the tool boundary — not just suggesting it in the prompt —
        is what stops a small model from inventing ad-hoc key names: a
        disallowed key returns a corrective message naming the permitted keys,
        so the model retries with the right handle. ``None`` = unrestricted.
        """
        from lazybridge import Tool

        store = self.store
        w_hint = f" You may write ONLY: {sorted(writable)}." if writable is not None else ""
        r_hint = f" You may read ONLY: {sorted(readable)}." if readable is not None else ""

        def bb_put(key: str, value: str) -> str:
            if writable is not None and key not in writable:
                return f"blackboard: key {key!r} not permitted. You may write ONLY: {sorted(writable)}"
            store.write(key, value)
            return f"blackboard: wrote {key!r}"

        def bb_get(key: str) -> str:
            if readable is not None and key not in readable:
                return f"blackboard: key {key!r} not permitted. You may read ONLY: {sorted(readable)}"
            val = store.read(key, "")
            return "" if val is None else str(val)

        def bb_list() -> str:
            keys = store.keys()
            return ", ".join(keys) if keys else "(blackboard empty)"

        return [
            Tool.wrap(
                bb_put,
                name="bb_put",
                description=(
                    "Write a short handle/value to the shared blackboard for other "
                    "specialists. Args: key (str), value (a SHORT string — a key, path, "
                    "number, or compact JSON; never bulk data)." + w_hint
                ),
            ),
            Tool.wrap(
                bb_get,
                name="bb_get",
                description=(
                    "Read a value from the shared blackboard. Args: key (str). Returns "
                    "the value, or empty if absent." + r_hint
                ),
            ),
            Tool.wrap(bb_list, name="bb_list", description="List the keys currently on the shared blackboard."),
        ]

    def read_tools(self, readable: set[str] | None = None) -> list[Any]:
        """Only the read side of the blackboard (bb_get scoped to ``readable`` + bb_list)."""
        return [t for t in self.as_tools(readable=readable, writable=set()) if t.name in {"bb_get", "bb_list"}]

    def publish_tool(self, fields: tuple[str, ...]) -> Any:
        """A single typed tool that writes a skill's declared output handles.

        Turning the ``writes`` set into named parameters of one ``publish`` call
        makes producing the contract handles a single structured step the model
        can't misname or forget — far more reliable with a small model than
        remembering a separate free-form write per handle.
        """
        from lazybridge import Tool

        store = self.store

        def publish(**handles: str) -> str:
            written = []
            for k in fields:
                v = handles.get(k)
                if v not in (None, ""):
                    store.write(k, v)
                    written.append(k)
            missing = [k for k in fields if k not in written]
            msg = f"blackboard: published {written}"
            return msg + (f"; still missing {missing}" if missing else "")

        publish.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [inspect.Parameter(f, inspect.Parameter.KEYWORD_ONLY, annotation=str) for f in fields]
        )
        return Tool.wrap(
            publish,
            name="publish",
            description=(
                "Publish your result handles to the shared blackboard in ONE call — "
                "this is how downstream specialists receive your output. Call it as "
                f"your final step. Fields (all required): {list(fields)}."
            ),
        )


# --------------------------------------------------------------------------- #
# Skill contract
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Skill:
    """A specialist capability: the contract plus the know-how to fulfil it.

    ``summary``/``when_to_use``/``reads``/``writes`` compose into the
    :meth:`description` an orchestrator sees when this skill is exposed as a
    tool — that description is how the orchestrator *knows what the skill
    does*. ``system`` is the specialist's system prompt (the composition
    know-how). ``build_tools`` returns the domain tool providers for the
    specialist, given an :class:`AnalystConfig`.
    """

    name: str
    summary: str
    when_to_use: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    system: str
    build_tools: Callable[[AnalystConfig], list[Any]] = field(repr=False)

    def description(self) -> str:
        reads = ", ".join(self.reads) or "nothing"
        writes = ", ".join(self.writes) or "nothing"
        return (
            f"{self.summary} Use when: {self.when_to_use} "
            f"Reads from the shared blackboard: {reads}. "
            f"Writes to the shared blackboard: {writes}."
        )

    def agent(self, engine: Any, cfg: AnalystConfig, blackboard: Blackboard, *, session: Any = None) -> Any:
        """Build the specialist :class:`~lazybridge.Agent` for this skill."""
        from lazybridge import Agent

        # read inputs via a scoped bb_get; produce outputs via one typed publish.
        board_tools = [*blackboard.read_tools(readable=set(self.reads)), blackboard.publish_tool(self.writes)]
        return Agent(
            name=self.name,
            engine=engine,
            tools=[*self.build_tools(cfg), *board_tools],
            description=self.description(),
            store=blackboard.store,
            session=session,
        )


@dataclass(frozen=True)
class AnalystConfig:
    """Wiring for the specialists' underlying tools.

    ``hub_db`` is the market-data-hub DuckDB (``None`` = its default);
    ``regime_db`` is the LazyStats depot file (created on first use);
    ``out_dir`` is the sandbox for saved reports and the base for the
    ``chart:`` / ``regimes:`` figure resolvers.
    """

    hub_db: str | None = None
    regime_db: str = "analyst_regimes.db"
    out_dir: str = "reports"


# --------------------------------------------------------------------------- #
# Domain-tool builders (one per specialist)
# --------------------------------------------------------------------------- #
def _market_data_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.connectors.datahub import DataHubTools

    return [DataHubTools(allow_refresh=True)]


def _financials_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.connectors.datahub import DataHubTools

    return [DataHubTools(allow_refresh=True)]


def _stats_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    return [StatisticalAnalysisTools()]


def _regime_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.connectors.regimes import RegimeTools

    return [RegimeTools(allow_write=True)]


def _report_tools(cfg: AnalystConfig) -> list[Any]:
    from lazytools.report import ReportFiles, ReportTools, ecosystem_resolvers

    files = ReportFiles(base_dir=cfg.out_dir)
    resolvers = ecosystem_resolvers(
        datahub_db_path=cfg.hub_db,
        regimes_db=cfg.regime_db,
        file_base_dir=cfg.out_dir,
    )
    return [ReportTools(artifacts=resolvers, files=files), files]


# --------------------------------------------------------------------------- #
# The five skills
# --------------------------------------------------------------------------- #
MARKET_DATA = Skill(
    name="market_data",
    summary="Ensures a stock's daily price history is in the warehouse.",
    when_to_use="you need price history for a ticker before any analysis.",
    reads=(),
    writes=("prices_ready",),
    system=(
        "You are the market-data specialist. Ensure the requested ticker's daily "
        "price history (from the requested start year) is present in the warehouse.\n"
        "- For a ticker that is NOT in the pre-configured universe you must FIRST "
        "call datahub_register_listing (exchange + currency required), THEN "
        "datahub_ensure_price_history. If ensure fails with an unknown-instrument "
        "error, register first and retry.\n"
        "- When the data is in place, call publish(prices_ready='<TICKER> daily "
        "from <start>').\n"
        "Do not fetch raw price rows into your context; only confirm availability."
    ),
    build_tools=_market_data_tools,
)

FINANCIALS = Skill(
    name="financials",
    summary="Fetches a company's SEC balance sheet and income lines.",
    when_to_use="the report needs balance-sheet / income figures.",
    reads=(),
    writes=("balance_sheet",),
    system=(
        "You are the financial-statements specialist. Ensure the ticker's SEC "
        "filings and XBRL facts are ingested (datahub_ensure_financials), then read "
        "the standardized annual balance sheet (datahub_get_statement with "
        "statement='balance') and the income lines (datahub_get_statement without a "
        "statement filter: revenue, net_income, operating_cash_flow).\n"
        "Call publish(balance_sheet=<COMPACT JSON with the main items and the "
        "period>), e.g. "
        "{\"period\":\"2025-12-31\",\"assets\":..,\"liabilities\":..,\"equity\":..,"
        "\"revenue\":..,\"net_income\":..,\"operating_cash_flow\":..}. Numbers only, "
        "no prose."
    ),
    build_tools=_financials_tools,
)

STATS = Skill(
    name="stats",
    summary="Computes volatility and return outliers over a window.",
    when_to_use="the report needs volatility / outlier statistics.",
    reads=("prices_ready",),
    writes=("vola_outlier",),
    system=(
        "You are the statistics specialist. For the requested ticker compute the "
        "annualized volatility and the return outliers over the requested window "
        "(daily frequency) with statistical_return_volatility and "
        "statistical_return_outliers. These tools read the series from the warehouse "
        "themselves, so confirm 'prices_ready' is on the blackboard first.\n"
        "Call publish(vola_outlier=<compact JSON with annualized_volatility, "
        "period_volatility, observations, total_outliers, and the few most extreme "
        "outliers>)."
    ),
    build_tools=_stats_tools,
)

REGIME = Skill(
    name="regime",
    summary="Fits a 3-regime volatility HMM over the full history and plots it.",
    when_to_use="the report needs a volatility-regime model and its chart.",
    reads=("prices_ready",),
    writes=("regime_result_key", "regime_plot_key", "regime_summary"),
    system=(
        "You are the regime-modelling specialist. Fit a hidden Markov model of "
        "volatility regimes on the ticker's FULL daily-return history:\n"
        "1. regime_load_from_datahub(symbols=<ticker>, start=<full start>, "
        "frequency='D', data_key='ret') — load daily returns into the depot. Plots "
        "require a data_key, so never fit inline data.\n"
        "2. regime_fit(data_key='ret', result_key='hmm', model='panel', S_min=3, "
        "S_max=3, ...) — S_min=S_max=3 forces exactly three regimes.\n"
        "3. regime_generate_plots(result_key='hmm', points_per_year=252, "
        "last_years=100) — take the plot_key whose name contains "
        "'__series_with_regimes__' (the price-with-regime-bands chart).\n"
        "4. regime_params_load and regime_get_current for the estimated parameters "
        "and current regime.\n"
        "Do NOT export any plot to disk (do not call regime_db_export_plot): the "
        "report embeds the chart straight from the depot via its plot_key.\n"
        "As your FINAL step call publish(regime_result_key='hmm', "
        "regime_plot_key=<the __series_with_regimes__ plot_key from step 3>, "
        "regime_summary=<compact JSON: per-regime mean/vol/occupancy, current regime "
        "and its probability, BIC>). This single call is how the report receives your "
        "output."
    ),
    build_tools=_regime_tools,
)

REPORT = Skill(
    name="report",
    summary="Assembles the memo and saves a self-contained HTML report.",
    when_to_use="all analyses are done and it is time to publish the report.",
    reads=("balance_sheet", "vola_outlier", "regime_summary", "regime_plot_key"),
    writes=("report_path",),
    system=(
        "You are the reporting specialist. Read the handles balance_sheet, "
        "vola_outlier, regime_summary and regime_plot_key from the blackboard (use "
        "bb_get with those exact key names), then compose a memo and SAVE it in ONE "
        "step with save_memo_html(memo=<memo>, filename='report.html'). Always use "
        "the filename 'report.html'. Never use render_memo_html then save_report, and "
        "never save to Markdown: an embedded-image HTML is too large to pass on and "
        "would be truncated.\n"
        "The memo shape is {title, as_of, sections:[{title, body, tables:[{columns, "
        "rows}], figures:[{ref, caption}]}], metadata}. Include:\n"
        "- an overview section with a figure of last month's price, ref "
        "'chart:symbols=<TICKER>&start=<~1 month ago>&end=<today>&field=adj_close&"
        "transform=level&frequency=D';\n"
        "- a volatility/outlier section (table from vola_outlier);\n"
        "- a regime section: a table of the three regimes and the transition matrix "
        "from regime_summary, plus a figure whose ref is exactly 'regimes:' followed "
        "by the regime_plot_key handle value (never a file: ref);\n"
        "- a balance-sheet section (table from balance_sheet) with a short comment on "
        "solidity, profitability and liquidity.\n"
        "All table cells must be strings. Finally call publish(report_path=<the path "
        "returned by save_memo_html>)."
    ),
    build_tools=_report_tools,
)

SKILLS: tuple[Skill, ...] = (MARKET_DATA, FINANCIALS, STATS, REGIME, REPORT)


# --------------------------------------------------------------------------- #
# Building the specialists
# --------------------------------------------------------------------------- #
def build_specialists(
    *,
    model: str | None = None,
    engine: Any = None,
    system: str = "Follow your skill's instructions exactly; be terse and call tools one at a time.",
    max_turns: int = 30,
    cfg: AnalystConfig | None = None,
    store: Any = None,
    session: Any = None,
    skills: tuple[Skill, ...] = SKILLS,
) -> dict[str, Any]:
    """Build the specialist agents, all sharing one blackboard (Store).

    Pass ``model`` (a cheap tier is fine — each specialist has a narrow job) and
    every specialist gets its OWN engine — important, because a single shared
    engine instance would share one turn budget across the whole pipeline and
    starve the last specialists. ``max_turns`` bounds each specialist's tool loop
    (the regime specialist legitimately needs a dozen calls). Pass ``engine=`` an
    engine *instance* only for construction/tests (it is reused as-is).

    Returns a name→Agent dict. The regime depot is initialised here so the regime
    tools and the ``regimes:`` figure resolver point at the same file.
    """
    from lazybridge import LLMEngine, Store

    if engine is None and model is None:
        raise ValueError("build_specialists needs either model= (recommended) or engine=")

    cfg = cfg or AnalystConfig()
    store = store if store is not None else Store()
    blackboard = Blackboard(store)

    if any(s.name == "regime" for s in skills):
        try:
            import lazystats.regimes.db as _rdb

            _rdb.init_regime_db(cfg.regime_db)
        except ImportError as exc:  # pragma: no cover - needs the extra absent
            raise ImportError(
                "the 'regime' skill requires lazystats[regimes]: pip install "
                "'lazystats[regimes] @ git+https://github.com/selvaz/LazyStats.git'"
            ) from exc

    def _engine_for(skill: Skill) -> Any:
        # a fresh engine per specialist, carrying the shared prefix PLUS the
        # skill's own system prompt (its know-how) — the whole point of a skill.
        # The engine= instance path is for construction/tests only; there the
        # per-skill prompt cannot be injected, so use model= to run.
        if engine is not None:
            return engine
        assert model is not None  # guaranteed above
        return LLMEngine(model, system=f"{system}\n\n{skill.system}", max_turns=max_turns)

    return {s.name: s.agent(_engine_for(s), cfg, blackboard, session=session) for s in skills}


def roster(skills: tuple[Skill, ...] = SKILLS) -> str:
    """One line per skill (name — description); handy to drop into a prompt."""
    return "\n".join(f"- {s.name}: {s.description()}" for s in skills)


# --------------------------------------------------------------------------- #
# Three orchestrators over the SAME specialists
# --------------------------------------------------------------------------- #
_PIPELINE_ORDER = ("market_data", "financials", "stats", "regime", "report")


def _step_task(name: str, ticker: str, start: str) -> str:
    tasks = {
        "market_data": f"Ensure daily price history for {ticker} from {start} is available.",
        "financials": f"Fetch and post {ticker}'s latest balance-sheet and income figures.",
        "stats": f"Compute year-to-date daily volatility and outliers for {ticker}.",
        "regime": f"Fit a 3-regime volatility HMM for {ticker} on the full history and plot it.",
        "report": f"Assemble and save the quantitative HTML report for {ticker}.",
    }
    return tasks[name]


def plan_orchestrator(
    specialists: dict[str, Any],
    *,
    ticker: str,
    start: str = "2015-01-01",
    name: str = "analyst_plan",
    session: Any = None,
) -> Any:
    """Deterministic pipeline: run the specialists in fixed dependency order.

    Data flows through the shared blackboard, so each step is just an
    instruction to the matching specialist; no sentinel threading is needed.
    """
    from lazybridge import Agent, Plan, Step

    order = [n for n in _PIPELINE_ORDER if n in specialists]
    steps = [Step(n, task=_step_task(n, ticker, start)) for n in order]
    return Agent(
        name=name,
        engine=Plan(*steps),
        tools=[specialists[n] for n in order],
        description="Deterministic analyst pipeline over the specialist skills.",
        session=session,
    )


BLACKBOARD_SYSTEM = (
    "You orchestrate a team of specialist skills to produce a quantitative equity "
    "report. Read each skill's description to know what it does and which blackboard "
    "handles it needs and produces, then plan 4-6 coarse tasks that respect those "
    "dependencies (prices must exist before statistics or regimes; the report comes "
    "last). Delegate each task to the right specialist; do not do their work "
    "yourself. The specialists exchange data through the shared blackboard — you only "
    "route."
)


def blackboard_orchestrator(
    specialists: dict[str, Any],
    *,
    model: str,
    name: str = "analyst_blackboard",
) -> Any:
    """Flat to-do-list orchestrator: the planner picks the next ready task."""
    from lazybridge.ext.planners import make_blackboard_planner

    return make_blackboard_planner(
        list(specialists.values()), model=model, system=BLACKBOARD_SYSTEM, name=name
    )


REPLAN_SYSTEM = (
    "You are the planner for a team of specialist skills producing a quantitative "
    "equity report. Each round you receive the available tools (specialists) with "
    "their descriptions, the goal, and the history so far. Emit a PlanRound: a short "
    "reasoning and the next task(s) to dispatch, each naming a specialist tool and "
    "its kwargs. Respect dependencies via the blackboard handles named in each "
    "specialist's description (e.g. prices_ready before stats/regime; the report "
    "last). If a step failed, read the error in the history and re-plan (for example, "
    "register a new ticker before retrying ingestion). Set done=true with a "
    "final_answer only once the report has been saved."
)


def replan_orchestrator(
    specialists: dict[str, Any],
    *,
    planner_model: str,
    max_rounds: int = 14,
    name: str = "analyst_replan",
    session: Any = None,
) -> Any:
    """Adaptive orchestrator: a planner + a re-planning loop over the specialists."""
    from lazybridge import Agent, LLMEngine, ReplanEngine
    from lazybridge.engines.replan import PlanRound

    planner = Agent(
        name="planner",
        engine=LLMEngine(planner_model, system=REPLAN_SYSTEM),
        output=PlanRound,
        session=session,
    )
    return Agent(
        name=name,
        engine=ReplanEngine(max_rounds=max_rounds),
        tools=[planner, *specialists.values()],
        description="Adaptive analyst orchestrator (plan → execute → observe → replan).",
        session=session,
    )
