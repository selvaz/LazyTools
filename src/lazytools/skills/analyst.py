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

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(
                self._put,
                name="bb_put",
                description=(
                    "Write a short handle/value to the shared blackboard for other "
                    "specialists to read. Args: key (str), value (a SHORT string — a "
                    "key, path, number, or compact JSON; never bulk data)."
                ),
            ),
            Tool.wrap(
                self._get,
                name="bb_get",
                description=(
                    "Read a value from the shared blackboard. Args: key (str). "
                    "Returns the value, or an empty string if absent."
                ),
            ),
            Tool.wrap(
                self._list,
                name="bb_list",
                description="List the keys currently on the shared blackboard.",
            ),
        ]

    def _put(self, key: str, value: str) -> str:
        self.store.write(key, value)
        return f"blackboard: wrote {key!r}"

    def _get(self, key: str) -> str:
        val = self.store.read(key, "")
        return "" if val is None else str(val)

    def _list(self) -> str:
        keys = self.store.keys()
        return ", ".join(keys) if keys else "(blackboard empty)"


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

        return Agent(
            name=self.name,
            engine=engine,
            tools=[*self.build_tools(cfg), *blackboard.as_tools()],
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
        "- When the data is in place, write the handle 'prices_ready' to the "
        "blackboard with a short value like '<TICKER> daily from <start>'.\n"
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
        "Write a COMPACT JSON handle 'balance_sheet' to the blackboard with the main "
        "items and the period, e.g. "
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
        "Write a compact JSON handle 'vola_outlier' with the key numbers "
        "(annualized_volatility, period_volatility, observations, total_outliers, and "
        "the few most extreme outliers)."
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
        "Write handles: 'regime_result_key'='hmm', 'regime_plot_key'=<that plot_key>, "
        "and 'regime_summary'=<compact JSON: per-regime mean/vol/occupancy, current "
        "regime and its probability, BIC>."
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
        "vola_outlier, regime_summary and regime_plot_key from the blackboard, then "
        "compose a memo and SAVE it in one step with save_memo_html(memo=<memo>, "
        "filename='report.html'). Never use render_memo_html then save_report: an "
        "embedded-image HTML is too large to pass on and would be truncated.\n"
        "The memo shape is {title, as_of, sections:[{title, body, tables:[{columns, "
        "rows}], figures:[{ref, caption}]}], metadata}. Include:\n"
        "- an overview section with a figure of last month's price, ref "
        "'chart:symbols=<TICKER>&start=<~1 month ago>&end=<today>&field=adj_close&"
        "transform=level&frequency=D';\n"
        "- a volatility/outlier section (table from vola_outlier);\n"
        "- a regime section: a table of the three regimes and the transition matrix "
        "from regime_summary, plus a figure with ref 'regimes:<regime_plot_key>';\n"
        "- a balance-sheet section (table from balance_sheet) with a short comment on "
        "solidity, profitability and liquidity.\n"
        "All table cells must be strings. Finally write the returned path to the "
        "blackboard as 'report_path'."
    ),
    build_tools=_report_tools,
)

SKILLS: tuple[Skill, ...] = (MARKET_DATA, FINANCIALS, STATS, REGIME, REPORT)


# --------------------------------------------------------------------------- #
# Building the specialists
# --------------------------------------------------------------------------- #
def build_specialists(
    *,
    engine: Any,
    cfg: AnalystConfig | None = None,
    store: Any = None,
    session: Any = None,
    skills: tuple[Skill, ...] = SKILLS,
) -> dict[str, Any]:
    """Build the specialist agents, all sharing one blackboard (Store).

    ``engine`` is used for every specialist (typically a cheap tier — each has a
    narrow job). Returns a name→Agent dict. The regime depot is initialised here
    so the regime tools and the ``regimes:`` figure resolver point at the same
    file.
    """
    from lazybridge import Store

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

    return {s.name: s.agent(engine, cfg, blackboard, session=session) for s in skills}


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
