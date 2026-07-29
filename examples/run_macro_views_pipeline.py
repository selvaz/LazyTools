"""Live test of the macro -> market -> BL views -> report -> Telegram pipeline.

Five specialists over ``lazytools.skills.macro_views``:
  A. macro   (cheap deepseek-v4-flash) -- news + macro data -> macro_thesis
  B. market  (cheap deepseek-v4-flash) -- regime/vol/correlation + per-asset
     regime charts -> market_state
  C. view_synthesis (cheap orchestrator, but the actual views come from Claude
     via the local ``claude_code`` CLI tool -- no ANTHROPIC_API_KEY needed,
     just an already-authenticated ``claude`` CLI on PATH)
  D. report  (cheap deepseek-v4-flash) -- exhaustive self-contained HTML memo
     (macro backdrop, market state, per-asset-class qual+quant analysis with
     embedded regime charts, views table) -> report_path
  E. telegram_delivery -- sends that file via telegram_send_document when
     TELEGRAM_BOT_TOKEN/TELEGRAM_OWNER_ID(or _CHAT_ID) are set; otherwise a
     no-op that says so

This does NOT touch the optimizer -- it stops at delivering the report.
Turning views_json into LazyPortfolio V2View tuples and wiring views/
view_tau through PortfolioOptimizationTools/PortfolioTreeTools is a
separate, later step (macro_views.MacroView mirrors V2View's fields 1:1).

Cost note: steps A/B/D/E are cheap (deepseek). Step C shells out to the
Claude Code CLI in read mode pinned to the "opus" model -- that's a real
Claude call through your CLI's own login, not free, and can take a minute
or two. Step E sends a real Telegram message if credentials are set.

Run from Spyder (F5) or the LazyTools checkout::

    C:\\ProgramData\\spyder-6\\python.exe examples\\run_macro_views_pipeline.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LAZYTOOLS_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = LAZYTOOLS_ROOT.parent


def _prefer_workspace_sources() -> None:
    for path in (
        WORKSPACE_ROOT / "LazyBridge",
        LAZYTOOLS_ROOT / "src",
        WORKSPACE_ROOT / "LazyStats" / "src",
        WORKSPACE_ROOT / "LazyCrawler",
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


def _resolve_news_db() -> str | None:
    candidate = WORKSPACE_ROOT / "LazyCrawler" / "news.db"
    return str(candidate) if candidate.is_file() else None


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _prefer_workspace_sources()
    _load_deepseek_key()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY missing (env or workspace deepseek.env).")
    hub_db = _resolve_db()
    news_db = _resolve_news_db()
    if news_db is None:
        print("NOTE: LazyCrawler/news.db not found -- macro specialist will run off macro/indicator data only.")

    from lazybridge import Session, Store
    from lazytools.skills.analyst import AnalystConfig
    from lazytools.skills.macro_views import build_macro_view_specialists, macro_views_plan

    model = os.getenv("LB_LIVE_DEEPSEEK_MODEL", "deepseek-v4-flash")
    # Multi-asset-class universe, all pre-verified present in market-data-hub
    # (datahub_get_coverage, 2026-07-28): US equity broad + tech + sector,
    # global equity, govt bonds (long/intermediate), IG/HY credit, gold,
    # broad commodities, REITs.
    universe = ["SPY", "QQQ", "XLE", "ACWI", "TLT", "IEF", "HYG", "LQD", "GLD", "DBC", "VNQ"]

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_OWNER_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not (telegram_token and telegram_chat_id):
        print("NOTE: TELEGRAM_BOT_TOKEN/TELEGRAM_OWNER_ID(or _CHAT_ID) not set -- telegram_delivery will no-op.")

    out_dir = WORKSPACE_ROOT / "market-pulse-agent" / "macro_views_reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    store = Store()
    session = Session(db=str(WORKSPACE_ROOT / "market-pulse-agent" / "macro_views_pipeline_events.db"), console=True)
    cfg = AnalystConfig(
        hub_db=hub_db,
        regime_db="macro_views_regimes.db",
        news_db=news_db,
        out_dir=str(out_dir),
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
    )

    specialists = build_macro_view_specialists(model=model, cfg=cfg, store=store, session=session)
    pipeline = macro_views_plan(specialists, universe=universe, session=session)

    print(f"Running macro_views_plan for {universe} (model={model}, view_synthesis -> claude_code/opus)...")
    result = pipeline(f"Generate Black-Litterman views for: {', '.join(universe)}.")
    if not result.ok:
        raise RuntimeError(f"pipeline failed: {result.error}")

    print(f"\n{'='*70}\nDONE  cost=${result.metadata.cost_usd:.6f} latency={result.metadata.latency_ms:.0f}ms")
    print(f"\nmacro_thesis:\n{store.read('macro_thesis', '(missing)')}")
    print(f"\nmarket_state:\n{store.read('market_state', '(missing)')}")

    views_json = store.read("views_json", None)
    if not views_json:
        raise SystemExit("views_json was never published to the blackboard -- see the transcript above.")
    print(f"\nviews_json (raw from claude_code):\n{views_json}")
    try:
        parsed = json.loads(views_json)
        print(f"\nParsed {len(parsed.get('views', []))} view(s) OK:")
        for v in parsed.get("views", []):
            print(f"  - {v.get('instruments')} -> {v.get('expected_return')} @ confidence={v.get('confidence')}"
                  f"  ({v.get('rationale', '')[:100]})")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"views_json is not valid JSON ({exc}) -- inspect the raw text above.") from exc

    report_path = store.read("report_path", None)
    print(f"\nreport_path: {report_path or '(missing -- report step did not publish)'}")
    print(f"telegram_status: {store.read('telegram_status', '(missing -- telegram_delivery did not publish)')}")


if __name__ == "__main__":
    main()
