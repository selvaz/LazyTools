"""Live DeepSeek smoke test for the Skfolio portfolio-optimizer tools.

Run from the LazyTools checkout::

    C:\\ProgramData\\spyder-6\\python.exe examples\\run_portfolio_optimization_deepseek.py

The agent is given only LazyTools providers and a portfolio-research task. It
selects the required data and portfolio-analysis tool sequence itself, then
creates an HTML report with OOS backtest figures and uses the Telegram provider
to deliver it. Historical returns remain inside market-data-hub. A compact
summary is sent to the chat configured by ``TELEGRAM_BOT_TOKEN`` and
``TELEGRAM_CHAT_ID``; the self-contained HTML report is uploaded as an
attachment.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

LAZYTOOLS_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = LAZYTOOLS_ROOT.parent


def _prefer_workspace_sources() -> None:
    """Make the example runnable from Spyder without editable installs."""
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
    """Use the process environment first, then the local development key file."""
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


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _prefer_workspace_sources()
    _load_deepseek_key()

    required = ("DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing required environment configuration: {', '.join(missing)}")

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
    os.environ["MARKET_DATA_DB"] = str(hub_db)

    from lazybridge import Agent, LLMEngine, Session
    from lazyfin.optimization import OptimizationStore

    from lazytools.connectors.datahub import DataHubTools
    from lazytools.connectors.fin import PortfolioOptimizationTools
    from lazytools.connectors.telegram import TelegramClient, TelegramTools
    from lazytools.report import ReportFiles, ReportTools, ecosystem_resolvers

    model = os.getenv("LB_LIVE_DEEPSEEK_MODEL", "deepseek-v4-flash")
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    prompt = f"""
Sei un agente di portfolio research. Decidi in autonomia quali tool chiamare,
in quale ordine e quali metodi comparare; non fare calcoli a mano e non
chiedere mai serie storiche.

Obiettivo: valuta un portafoglio strategico a quattro pillar (equity, oro,
fixed income e Bloomberg Commodity Index). Usa SPY, GLD e TLT; per il pillar
commodity usa BCI, un ETF negoziabile che replica BCOM total return. Assicurati
che i dati necessari siano disponibili attraverso gli strumenti del data hub.

Usa SPY/TLT 70/30 come benchmark statico di confronto, non come universo
investibile. Confronta in modo ragionato le policy disponibili sullo stesso
protocollo walk-forward: finestra rolling di circa tre anni, ribilanciamento
trimestrale, un giorno di separazione tra stima ed esecuzione e pesi long-only
senza una posizione oltre il 60%. Il benchmark non deve diventare un vincolo di
tracking error, salvo tu possa motivarlo nel report.

Prepara un report HTML auto-contenuto in italiano: metodo e protocollo,
confronto delle metriche aggregate con il benchmark, limiti del test e almeno
un grafico OOS di backtesting ottenuto dai tool. Salvalo nell'area consentita.
Manda alla chat Telegram '{chat_id}' un singolo messaggio di sintesi, poi
allega il report HTML. Non includere dati grezzi, serie, immagini codificate o
report intero nel messaggio o nella risposta finale.
""".strip()

    with tempfile.TemporaryDirectory(prefix="lazytools-portfolio-live-") as temporary_dir:
        output_dir = Path(temporary_dir)
        session = Session(db=str(output_dir / "session.sqlite"))
        try:
            report_files = ReportFiles(base_dir=output_dir)
            report_tools = ReportTools(artifacts=ecosystem_resolvers(file_base_dir=str(output_dir)), files=report_files)
            with TelegramClient.from_token(os.environ["TELEGRAM_BOT_TOKEN"]) as telegram_client:
                agent = Agent(
                    name="portfolio_optimizer_live_smoke",
                    engine=LLMEngine(
                        model,
                        system=(
                            "You are an autonomous portfolio-research agent. Select and sequence the "
                            "supplied tools to complete the task. Historical return observations must "
                            "remain inside the tool process. The Telegram allow-list is the configured "
                            "chat; send only the requested summary and one report attachment."
                        ),
                        max_tool_calls_per_turn=14,
                    ),
                    tools=[
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
                result = agent(prompt)
            if not result.ok:
                raise RuntimeError(f"DeepSeek agent failed: {result.error}")

            called_tools = {
                event["payload"].get("tool")
                for event in session.events.query()
                if event.get("event_type") == "tool_call"
            }
            if "portfolio_optimizer_backtest" not in called_tools:
                raise RuntimeError("agent completed without an optimizer backtest")
            if "save_memo_html" not in called_tools or not list(output_dir.glob("*.html")):
                raise RuntimeError("agent completed without a persisted HTML report")
            if not {"telegram_send_message", "telegram_send_document"} <= called_tools:
                raise RuntimeError("agent completed without the requested Telegram delivery")

            print("Live portfolio-optimizer and Telegram tool test passed.")
            print(f"Model: {model}")
            print(f"Tools invoked: {', '.join(sorted(called_tools))}")
            print(f"Cost: ${result.metadata.cost_usd:.6f} | latency: {result.metadata.latency_ms:.0f} ms")
            print("\nAgent response:\n")
            print(result.text())
        finally:
            session.close()


if __name__ == "__main__":
    main()
