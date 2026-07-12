"""Autonomous quantitative equity report from composable *analyst skills*.

Five specialist skills (market data, financials, statistics, volatility regimes,
report) each = domain tools + a tailored system prompt, sharing one blackboard.
The same specialists are driven by three interchangeable orchestrators — a
deterministic Plan, a blackboard planner, or an adaptive replan loop — selected
with the ``LB_ORCH`` env var (default: ``plan``).

Run from Spyder (F5) or the LazyTools checkout::

    C:\\ProgramData\\spyder-6\\python.exe examples\\analyst_report.py
    LB_ORCH=blackboard  C:\\ProgramData\\spyder-6\\python.exe examples\\analyst_report.py

It makes real, low-cost DeepSeek ``deepseek-v4-flash`` calls. Needs a running
market-data-hub warehouse (its DuckDB) and network access (Yahoo + SEC). The
specialists exchange only short handles over the blackboard; heavy artifacts
stay in the depot/DB/disk. The final report path is read back from the
blackboard and printed.

Note: the deterministic ``plan`` orchestrator is the most reliable on the cheap
model; ``blackboard``/``replan`` exercise the model's own routing/planning and
benefit from a stronger tier (set ``LB_ORCH_MODEL=deepseek-v4-pro``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless regime plots

LAZYTOOLS_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = LAZYTOOLS_ROOT.parent


def _prefer_workspace_sources() -> None:
    for path in (
        WORKSPACE_ROOT / "LazyBridge",
        LAZYTOOLS_ROOT / "src",
        WORKSPACE_ROOT / "market-data-hub",
        WORKSPACE_ROOT / "LazyStats" / "src",
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


def main() -> None:
    _prefer_workspace_sources()
    _load_deepseek_key()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("Set DEEPSEEK_API_KEY or put a bare key in ../deepseek.env")

    from lazybridge import Session, Store

    from lazytools.skills import (
        AnalystConfig,
        blackboard_orchestrator,
        build_specialists,
        plan_orchestrator,
        replan_orchestrator,
        roster,
    )

    ticker = os.getenv("LB_TICKER", "AMZN")
    which = os.getenv("LB_ORCH", "plan").lower()
    specialist_model = os.getenv("LB_MODEL", "deepseek-v4-flash")
    orch_model = os.getenv("LB_ORCH_MODEL", specialist_model)

    hub_db = str(WORKSPACE_ROOT / "market-data-hub" / "market_data.duckdb")
    os.environ.setdefault("MARKET_DATA_DB", hub_db)
    out_dir = WORKSPACE_ROOT / "reports_demo"
    out_dir.mkdir(exist_ok=True)
    cfg = AnalystConfig(
        hub_db=hub_db,
        regime_db=str(out_dir / "analyst_skills_regimes.db"),
        out_dir=str(out_dir),
    )

    # one shared blackboard (Store) + one shared Session (events for the viz)
    store = Store()
    session = Session(db=str(out_dir / "analyst_events.db"), console=True)

    print("Skills available to the orchestrator:")
    print(roster())
    print(f"\nOrchestrator: {which}   ticker: {ticker}\n")

    specialists = build_specialists(
        model=specialist_model,  # each specialist gets its own engine (own turn budget)
        cfg=cfg,
        store=store,
        session=session,
    )

    goal = (
        f"Produce and save a self-contained HTML quantitative report on {ticker}: "
        "a last-month price chart; the latest balance sheet with a short comment on "
        "solidity, profitability and liquidity; year-to-date daily volatility and "
        "outliers; and a 3-regime volatility HMM fitted over the full price history "
        "(from 2015) with its parameters and a price-with-regimes chart."
    )

    if which == "plan":
        orchestrator = plan_orchestrator(specialists, ticker=ticker, session=session)
    elif which == "blackboard":
        orchestrator = blackboard_orchestrator(specialists, model=orch_model)
    elif which == "replan":
        orchestrator = replan_orchestrator(specialists, planner_model=orch_model, session=session)
    else:
        raise SystemExit(f"Unknown LB_ORCH={which!r}; use plan | blackboard | replan")

    try:
        env = orchestrator(goal)
        print("\n=== ORCHESTRATOR FINAL MESSAGE ===")
        print(env.text().encode("ascii", "replace").decode("ascii"))

        report_path = store.read("report_path", "")
        handles = [k for k in store.keys() if not k.startswith("__agent_output__")]  # noqa: SIM118
        print("\nBlackboard handles:", ", ".join(handles) or "(none)")
        candidate = Path(str(report_path)) if report_path else None
        if not (candidate and candidate.exists()):
            saved = sorted(out_dir.glob("*.html"), key=lambda q: q.stat().st_mtime, reverse=True)
            candidate = saved[0] if saved else None
        if candidate and candidate.exists():
            html = candidate.read_text(encoding="utf-8", errors="replace")
            figs = html.count("data:image/png;base64,")
            print(f"Report saved: {candidate}  ({candidate.stat().st_size:,} bytes, {figs} embedded figures)")
        else:
            print("No report saved — inspect the tool calls above.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
