"""Live DeepSeek smoke test for the market-data-hub statistical tools.

Run this file from Spyder (Run file / F5) or from the LazyTools checkout::

    C:\\ProgramData\\spyder-6\\python.exe examples\\run_statistical_analysis_deepseek.py

It makes one real, low-cost DeepSeek ``deepseek-v4-flash`` call. The complete
return matrix stays inside ``StatisticalAnalysisTools``: the model sees only
the compact volatility/correlation/outlier results.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


LAZYTOOLS_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = LAZYTOOLS_ROOT.parent


def _prefer_workspace_sources() -> None:
    """Make the script runnable from Spyder without an editable install."""
    for path in (
        WORKSPACE_ROOT / "LazyBridge",
        LAZYTOOLS_ROOT / "src",
        WORKSPACE_ROOT / "market-data-hub",
    ):
        text = str(path)
        if path.exists() and text not in sys.path:
            sys.path.insert(0, text)


def _load_deepseek_key() -> None:
    """Use the environment first, then the local bare-key ``deepseek.env`` file."""
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
    # Spyder already uses UTF-8, while a Windows console can still default to
    # cp1252. Reconfigure when possible so a model's Unicode punctuation does
    # not turn an otherwise successful live test into an exit-code failure.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _prefer_workspace_sources()
    _load_deepseek_key()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError(
            "DEEPSEEK_API_KEY is missing. Set it in Spyder's environment or put the bare key in "
            f"{WORKSPACE_ROOT / 'deepseek.env'}"
        )

    default_db = WORKSPACE_ROOT / "market-data-hub" / "market_data.duckdb"
    configured_db = os.environ.get("MARKET_DATA_DB")
    candidates = [default_db]
    if configured_db:
        configured_path = Path(configured_db).expanduser()
        candidates = [
            configured_path if configured_path.is_absolute() else Path.cwd() / configured_path,
            WORKSPACE_ROOT / configured_path,
            default_db,
        ]
    db_path = next((path.resolve() for path in candidates if path.is_file()), None)
    if db_path is None:
        searched = ", ".join(str(path) for path in candidates)
        raise FileNotFoundError(f"market-data-hub DuckDB not found. Searched: {searched}")
    os.environ["MARKET_DATA_DB"] = str(db_path)

    from lazybridge import Agent, LLMEngine, Session
    from lazytools.statistical_analysis import StatisticalAnalysisTools

    model = os.getenv("LB_LIVE_DEEPSEEK_MODEL", "deepseek-v4-flash")
    expected_tools = {
        "statistical_return_volatility",
        "statistical_return_correlation",
        "statistical_return_outliers",
    }
    prompt = """
Sei un agente QA. Devi verificare i tool statistici, non fare calcoli a mano.
Chiama tutti e tre i tool: statistical_return_volatility,
statistical_return_correlation e statistical_return_outliers. Per ogni tool usa
instruments='ticker:SPY,ticker:TLT', start='2024-01-01', end='2024-12-31' e
frequency='W'. Per gli outlier lascia threshold al suo valore predefinito.
Poi rispondi in italiano, in massimo quattro righe: volatilita' annualizzata di
SPY e TLT, correlazione SPY/TLT, numero degli outlier e la loro data piu'
estrema. Cita solo i risultati dei tool; non inventare dati e non chiedere la
serie grezza.
""".strip()

    with tempfile.TemporaryDirectory(prefix="lazytools-statistical-smoke-") as temporary_dir:
        session = Session(db=str(Path(temporary_dir) / "session.sqlite"))
        try:
            agent = Agent(
                name="statistical_analysis_smoke",
                engine=LLMEngine(
                    model,
                    system=(
                        "Use the supplied function tools exactly as instructed. "
                        "The tools return compact analysis results, never raw price or return series."
                    ),
                    max_tool_calls_per_turn=4,
                ),
                tools=[StatisticalAnalysisTools()],
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
            called_tools.discard(None)
            missing_tools = expected_tools - called_tools
            if missing_tools:
                raise RuntimeError(
                    "The agent completed without invoking every required statistical tool. "
                    f"Missing: {sorted(missing_tools)}; invoked: {sorted(called_tools)}"
                )

            print("Live statistical-analysis tool test passed.")
            print(f"Model: {model}")
            print(f"Tools invoked: {', '.join(sorted(called_tools))}")
            print(f"Cost: ${result.metadata.cost_usd:.6f} | latency: {result.metadata.latency_ms:.0f} ms")
            print("\nAgent response:\n")
            print(result.text())
        finally:
            session.close()


if __name__ == "__main__":
    main()
