"""Live DeepSeek test of the statistical specialists + supervisor.

Exercises every hub-backed statistical tool through the specialist agents and
then through the orchestrator, with real (low-cost) ``deepseek-v4-flash``
calls. All market data is read from market-data-hub; the models see only the
compact tool results.

Run from Spyder (F5) or the LazyTools checkout::

    C:\\ProgramData\\spyder-6\\python.exe examples\\run_stats_agents_deepseek.py

Sections:
  A. volatility-correlation-analyst  -> vol / corr / outliers tools
  B. regression-analyst              -> OLS / Ridge / Lasso tools
  C. regime-analyst (allow_write)    -> load-from-hub / fit / summary tools
  D. stats-supervisor                -> delegates to the specialists (agent-as-tool)
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
        LAZYTOOLS_ROOT / "src",
        WORKSPACE_ROOT / "LazyStats" / "src",
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


def _resolve_db() -> str:
    default_db = WORKSPACE_ROOT / "market-data-hub" / "market_data.duckdb"
    configured = os.environ.get("MARKET_DATA_DB")
    candidates = [default_db]
    if configured:
        p = Path(configured).expanduser()
        candidates = [p if p.is_absolute() else Path.cwd() / p, WORKSPACE_ROOT / p, default_db]
    db = next((c.resolve() for c in candidates if c.is_file()), None)
    if db is None:
        raise FileNotFoundError("market-data-hub DuckDB not found: " + ", ".join(map(str, candidates)))
    return str(db)


def _called_tools(session) -> set[str]:
    tools = {
        event["payload"].get("tool")
        for event in session.events.query()
        if event.get("event_type") == "tool_call"
    }
    tools.discard(None)
    return tools


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _prefer_workspace_sources()
    _load_deepseek_key()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY missing (env or workspace deepseek.env).")
    os.environ["MARKET_DATA_DB"] = _resolve_db()

    from lazybridge import Session
    from lazytools.skills.stats_agents import (
        regime_analyst,
        regression_analyst,
        stats_supervisor,
        volatility_correlation_analyst,
    )

    model = os.getenv("LB_LIVE_DEEPSEEK_MODEL", "deepseek-v4-flash")
    UNIV = "ticker:SPY,ticker:TLT,ticker:GLD,ticker:QQQ"
    total_cost = 0.0
    results: list[tuple[str, bool, set[str], set[str]]] = []

    def run(label, agent, session, prompt, expected):
        nonlocal total_cost
        res = agent(prompt)
        if not res.ok:
            raise RuntimeError(f"{label}: agent failed: {res.error}")
        total_cost += float(res.metadata.cost_usd or 0.0)
        called = _called_tools(session)
        ok = expected.issubset(called)
        results.append((label, ok, expected - called, called))
        print(f"\n{'='*70}\n{label}  [{'PASS' if ok else 'INCOMPLETE'}]  "
              f"cost=${res.metadata.cost_usd:.6f} latency={res.metadata.latency_ms:.0f}ms")
        if expected - called:
            print(f"  missing expected tools: {sorted(expected - called)}")
        print(f"  tools invoked: {sorted(called)}")
        print(f"\n  {res.text().strip()[:900]}")

    with tempfile.TemporaryDirectory(prefix="stats-agents-smoke-") as td:
        # --- A: volatility / correlation / outliers -----------------------
        s_a = Session(db=str(Path(td) / "a.sqlite"))
        vc = volatility_correlation_analyst(model, session=s_a)
        run("A. volatility-correlation-analyst", vc, s_a,
            f"For instruments '{UNIV}' over 2015-01-01..2024-12-31 at weekly frequency, "
            "report annualised volatility of each, the SPY/TLT correlation, and how many "
            "return outliers SPY has (default threshold). Call every relevant tool.",
            {"statistical_return_volatility", "statistical_return_correlation",
             "statistical_return_outliers"})
        s_a.close()

        # --- B: OLS / Ridge / Lasso ------------------------------------------
        s_b = Session(db=str(Path(td) / "b.sqlite"))
        rg = regression_analyst(model, session=s_b)
        run("B. regression-analyst", rg, s_b,
            "Using dependent='SPY', regressors='TLT,GLD,QQQ', frequency='W', "
            "start='2015-01-01', end='2024-12-31': run an OLS (robust_se='HAC'), a "
            "Ridge and a Lasso. Report which factor dominates and whether Ridge or "
            "Lasso shrinks anything. Call all three regression tools.",
            {"statistical_regression_ols", "statistical_regression_ridge",
             "statistical_regression_lasso"})
        s_b.close()

        # --- C: regime detection (load -> fit -> summarise) --------------
        s_c = Session(db=str(Path(td) / "c.sqlite"))
        rga = regime_analyst(model, allow_write=True, session=s_c)
        run("C. regime-analyst", rga, s_c,
            "Model weekly volatility regimes for 'ticker:SPY' from 2010-01-01 to today: "
            "load returns from the hub, fit a 2-3 regime HMM, then report the number of "
            "regimes, each regime's volatility, and the current regime.",
            {"regime_load_from_datahub", "regime_fit", "regime_get_summary"})
        s_c.close()

        # --- D: supervisor delegating to the specialists -----------------
        s_d = Session(db=str(Path(td) / "d.sqlite"))
        sup = stats_supervisor(model, session=s_d, regime_allow_write=True)
        run("D. stats-supervisor (agent-as-tool)", sup, s_d,
            f"For the universe '{UNIV}', weekly, 2015-01-01..2024-12-31: (1) give SPY and "
            "TLT annualised volatility and their correlation, and (2) regress SPY on TLT, "
            "GLD and QQQ (OLS, HAC) and say which factor dominates. Delegate each part to "
            "the right specialist and synthesise.",
            {"volatility-correlation-analyst", "regression-analyst"})
        s_d.close()

    print(f"\n{'#'*70}\nSUMMARY  (model={model})")
    for label, ok, missing, _ in results:
        print(f"  [{'PASS' if ok else 'INCOMPLETE'}] {label}" + (f"  missing {sorted(missing)}" if missing else ""))
    print(f"  total cost: ${total_cost:.6f}")
    if not all(ok for _, ok, _, _ in results):
        raise SystemExit("Some sections did not invoke every expected tool (see above).")
    print("  ALL SECTIONS PASSED")


if __name__ == "__main__":
    main()
