"""Live portfolio research through LazyBridge's adaptive blackboard planner.

The host wires specialists and their tool boundaries only. The blackboard LLM
sets/revises the work plan, delegates research, then delegates HTML report and
Telegram delivery. Historical return observations never enter LLM context.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

LAZYTOOLS_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = LAZYTOOLS_ROOT.parent


def _prefer_workspace_sources() -> None:
    for path in (
        WORKSPACE_ROOT / "LazyBridge",
        WORKSPACE_ROOT / "LazyFin" / "src",
        LAZYTOOLS_ROOT / "src",
        WORKSPACE_ROOT / "market-data-hub",
    ):
        text = str(path)
        if path.exists() and text not in sys.path:
            sys.path.insert(0, text)


def _load_deepseek_key() -> None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    key_file = WORKSPACE_ROOT / "deepseek.env"
    if not key_file.exists():
        return
    value = key_file.read_text(encoding="utf-8").strip()
    if value.startswith("DEEPSEEK_API_KEY="):
        value = value.split("=", 1)[1].strip()
    if value:
        os.environ["DEEPSEEK_API_KEY"] = value


def _resolve_hub_db() -> Path:
    default_hub = WORKSPACE_ROOT / "market-data-hub" / "market_data.duckdb"
    configured_hub = os.environ.get("MARKET_DATA_DB")
    candidates = [default_hub]
    if configured_hub:
        configured_path = Path(configured_hub).expanduser()
        candidates = [
            configured_path if configured_path.is_absolute() else Path.cwd() / configured_path,
            WORKSPACE_ROOT / configured_path,
            default_hub,
        ]
    hub_db = next((path.resolve() for path in candidates if path.is_file()), None)
    if hub_db is None:
        raise FileNotFoundError(f"market-data-hub DuckDB not found. Searched: {', '.join(map(str, candidates))}")
    return hub_db


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _prefer_workspace_sources()
    _load_deepseek_key()

    required = ("DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing required environment configuration: {', '.join(missing)}")
    os.environ["MARKET_DATA_DB"] = str(_resolve_hub_db())

    from lazybridge import Agent, LLMEngine, Session, Tool
    from lazybridge.ext.planners import make_blackboard_planner
    from lazyfin.optimization import OptimizationStore

    from lazytools.connectors.datahub import DataHubTools
    from lazytools.connectors.fin import PortfolioOptimizationTools
    from lazytools.connectors.telegram import TelegramClient, TelegramTools
    from lazytools.report import ReportFiles, ReportTools, ecosystem_resolvers

    model = os.getenv("LB_LIVE_DEEPSEEK_MODEL", "deepseek-v4-pro")
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    task = f"""
Coordinate autonomous portfolio research on the four pillars SPY, GLD, TLT and
BCI, with SPY/TLT 70/30 as a static comparator. First delegate research to the
portfolio specialist. Then delegate report production and Telegram delivery to
the delivery specialist, passing it only the specialist's bounded findings and
exact chart references. Use a three-year rolling / quarterly-rebalance
walk-forward protocol using weekly returns: roughly 156 weeks rolling
estimation and monthly rebalancing every four weeks. Use no additional
purge/execution lag: weights estimated at a rebalance date apply to the
immediately following weekly return and remain in place for four weeks. Keep it
long-only and below 60% per position. Every portfolio_optimizer_run and
portfolio_optimizer_backtest in this task must use transaction_cost_bps=10.0,
uniformly for every instrument; do not run a zero-cost variant. State that this
is a 10 bps per-turnover modelling assumption and do not invent realized costs
not returned by the optimizer. The final report must be Italian HTML with
aggregate results, limitations, OOS chart(s), and a "Costi" section. That
section must separate the modeled portfolio transaction-cost assumptions from
the verified LLM/API usage obtained through get_session_usage.
Telegram chat id is {chat_id}; send one summary and the HTML attachment.
""".strip()

    with tempfile.TemporaryDirectory(prefix="lazytools-portfolio-blackboard-") as temporary_dir:
        output_dir = Path(temporary_dir)
        session = Session(db=str(output_dir / "session.sqlite"))
        try:
            report_files = ReportFiles(base_dir=output_dir)
            report_tools = ReportTools(artifacts=ecosystem_resolvers(file_base_dir=str(output_dir)), files=report_files)
            with TelegramClient.from_token(os.environ["TELEGRAM_BOT_TOKEN"]) as telegram_client:
                researcher = Agent(
                    name="portfolio_researcher",
                    description="Uses DataHub and Skfolio tools for bounded portfolio research and OOS charts.",
                    session=session,
                    engine=LLMEngine(
                        model,
                        system=(
                            "You are the portfolio specialist. Use only your supplied data and optimizer tools. "
                            "Choose appropriate available methods; never invent a method id. Keep returns internal. "
                            "Return a concise Italian handoff containing aggregate metrics, limitations and every "
                            "exact chart.ref needed by a report specialist. Do not create reports or send messages."
                        ),
                        max_tool_calls_per_turn=14,
                    ),
                    tools=[
                        DataHubTools(allow_refresh=True),
                        PortfolioOptimizationTools(
                            OptimizationStore(str(output_dir / "optimizer.sqlite")), artifacts_dir=output_dir
                        ),
                    ],
                )
                delivery = Agent(
                    name="report_delivery",
                    description="Builds an HTML report from bounded findings and delivers it through Telegram tools.",
                    session=session,
                    engine=LLMEngine(
                        model,
                        system=(
                            "You are the report and delivery specialist. Use only bounded findings and chart.ref values "
                            "in the task. Call get_session_usage once before writing the report and include its token and "
                            "USD cost telemetry in a distinct Costi section, clearly labelled as accrued up to that call. "
                            "Create a self-contained Italian HTML memo using save_memo_html, then use the Telegram tools "
                            "for exactly one short summary and the HTML attachment. Do not request or invent historical "
                            "observations or costs."
                        ),
                        max_tool_calls_per_turn=5,
                    ),
                    tools=[
                        report_tools,
                        Tool.wrap(
                            session.usage_summary,
                            name="get_session_usage",
                            description=(
                                "Return bounded LazyBridge token and USD-cost telemetry for this session, "
                                "aggregated by agent. It contains no market data."
                            ),
                        ),
                        TelegramTools(
                            telegram_client,
                            allowed_chat_ids=[chat_id],
                            require_confirmation=False,
                            attachments_dir=output_dir,
                        ),
                    ],
                )
                coordinator = make_blackboard_planner(
                    [researcher, delivery],
                    model=model,
                    system=(
                        "You are a blackboard coordinator. Use set_plan, get_plan and mark_done for this multi-step "
                        "task. Delegate analysis to portfolio_researcher first. After its bounded handoff, delegate the "
                        "report and Telegram delivery to report_delivery. If a specialist reports an error, revise the "
                        "plan and ask it to recover. Do not perform analysis, report creation or delivery yourself."
                    ),
                )
                result = coordinator(task)
            if not result.ok:
                raise RuntimeError(f"DeepSeek blackboard failed: {result.error}")

            called_tools = {
                event["payload"].get("tool")
                for event in session.events.query()
                if event.get("event_type") == "tool_call"
            }
            required_outcomes = {
                "portfolio_optimizer_backtest",
                "save_memo_html",
                "telegram_send_message",
                "telegram_send_document",
            }
            if missing_outcomes := required_outcomes - called_tools:
                raise RuntimeError(f"blackboard missed required outcomes: {sorted(missing_outcomes)}")
            if not list(output_dir.glob("*.html")):
                raise RuntimeError("blackboard completed without a persisted HTML report")

            print("Live blackboard portfolio test passed.")
            print(f"Model: {model}")
            print(f"Tools invoked: {', '.join(sorted(called_tools))}")
            print(f"Result: {result.text()}")
        finally:
            session.close()


if __name__ == "__main__":
    main()
