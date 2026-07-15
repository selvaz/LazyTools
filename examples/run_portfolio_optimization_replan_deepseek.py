"""Live adaptive portfolio research using LazyBridge ``ReplanEngine``.

The host only creates tool providers, a temporary artifact sandbox and a
session.  DeepSeek's planner selects each next tool round; it also creates the
HTML report and calls the Telegram tools.  No prices or return observations
cross into LLM context.

Run from the LazyTools checkout::

    C:\\ProgramData\\spyder-6\\python.exe examples\\run_portfolio_optimization_replan_deepseek.py
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
        searched = ", ".join(str(path) for path in candidates)
        raise FileNotFoundError(f"market-data-hub DuckDB not found. Searched: {searched}")
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

    from lazybridge import Agent, LLMEngine, ReplanEngine, Session
    from lazybridge.engines.replan import PlanRound
    from lazyfin.optimization import OptimizationStore

    from lazytools.connectors.datahub import DataHubTools
    from lazytools.connectors.fin import PortfolioOptimizationTools
    from lazytools.connectors.telegram import TelegramClient, TelegramTools
    from lazytools.report import ReportFiles, ReportTools, ecosystem_resolvers

    model = os.getenv("LB_LIVE_DEEPSEEK_MODEL", "deepseek-v4-flash")
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    planner = Agent(
        name="planner",
        output=PlanRound,
        engine=LLMEngine(
            model,
            system=(
                "You are an adaptive task planner. Emit one PlanRound. The current task, "
                "available tool schemas and compact prior-round results are supplied to you. "
                "Choose only available tools and exact kwargs. Put dependent work in later rounds; "
                "use parallelism only for independent read-only tasks. Do not finish until the task's "
                "report and Telegram delivery are complete."
            ),
        ),
    )
    task = f"""
Conduct autonomous portfolio research using only the supplied tools. Historical
observations must remain inside tools. The investment universe is SPY (equity),
GLD (gold), TLT (fixed income) and BCI (a tradeable BCOM total-return tracker).
Use SPY/TLT 70/30 as a static comparator, not as the investible universe.

Choose an appropriate set of available optimization policies and compare them
using one defensible walk-forward protocol: roughly three years rolling
estimation, quarterly rebalancing, one-observation execution lag, long-only,
and no position above 60%. Ensure any required data is available. Produce a
self-contained Italian HTML report with aggregate comparison, limitations and
at least one optimizer-generated OOS backtest chart. Then send one concise
summary and the HTML attachment to Telegram chat {chat_id}. End with a compact
Italian conclusion containing only aggregate results.
""".strip()

    with tempfile.TemporaryDirectory(prefix="lazytools-portfolio-replan-") as temporary_dir:
        output_dir = Path(temporary_dir)
        session = Session(db=str(output_dir / "session.sqlite"))
        try:
            report_files = ReportFiles(base_dir=output_dir)
            report_tools = ReportTools(artifacts=ecosystem_resolvers(file_base_dir=str(output_dir)), files=report_files)
            with TelegramClient.from_token(os.environ["TELEGRAM_BOT_TOKEN"]) as telegram_client:
                guardian = Agent(
                    name="portfolio_replan_guardian",
                    engine=ReplanEngine(max_rounds=8),
                    tools=[
                        planner,
                        DataHubTools(allow_refresh=True),
                        PortfolioOptimizationTools(
                            OptimizationStore(str(output_dir / "optimizer.sqlite")), artifacts_dir=output_dir
                        ),
                        report_tools,
                        TelegramTools(
                            telegram_client,
                            allowed_chat_ids=[chat_id],
                            require_confirmation=False,
                            attachments_dir=output_dir,
                        ),
                    ],
                    session=session,
                )
                result = guardian(task)
            if not result.ok:
                raise RuntimeError(f"DeepSeek replanner failed: {result.error}")

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
                raise RuntimeError(f"replanner missed required outcomes: {sorted(missing_outcomes)}")
            if not list(output_dir.glob("*.html")):
                raise RuntimeError("replanner completed without a persisted HTML report")

            print("Live ReplanEngine portfolio test passed.")
            print(f"Model: {model}")
            print(f"Tools invoked: {', '.join(sorted(called_tools))}")
            print(f"Result: {result.text()}")
        finally:
            session.close()


if __name__ == "__main__":
    main()
