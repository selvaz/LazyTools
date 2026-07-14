"""Live DeepSeek test of the charted statistical report pipeline.

Runs stats_report_pipeline (vol_corr -> regression -> regime -> stats_report)
with real deepseek-v4-flash calls, entirely over market-data-hub, and prints
the path + a size check of the resulting self-contained HTML report.
"""

from __future__ import annotations

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


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _prefer_workspace_sources()
    _load_deepseek_key()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("DEEPSEEK_API_KEY missing.")
    db = WORKSPACE_ROOT / "market-data-hub" / "market_data.duckdb"
    os.environ["MARKET_DATA_DB"] = str(db)

    from lazybridge import Session
    from lazytools.skills import AnalystConfig, stats_report_pipeline

    model = os.getenv("LB_LIVE_DEEPSEEK_MODEL", "deepseek-v4-flash")
    out_dir = LAZYTOOLS_ROOT / "reports_live_test"
    cfg = AnalystConfig(hub_db=str(db), regime_db=str(out_dir / "stats_regimes.db"), out_dir=str(out_dir))

    session = Session(db=str(out_dir / "session.sqlite"))
    pipeline = stats_report_pipeline(
        model=model, symbols="SPY,TLT,GLD,QQQ", dependent="SPY", regressors="TLT,GLD,QQQ",
        start="2015-01-01", end="2024-12-31", frequency="W", regime_start="2010-01-01",
        cfg=cfg, session=session,
    )
    result = pipeline("Go.")
    if not result.ok:
        raise RuntimeError(f"pipeline failed: {result.error}")

    print(f"cost=${result.metadata.cost_usd:.6f}  latency={result.metadata.latency_ms:.0f}ms")
    print("final payload:", result.text()[:500])

    html_files = sorted(out_dir.glob("*.html"))
    print("\nHTML reports written:", [str(p) for p in html_files])
    for p in html_files:
        size = p.stat().st_size
        text = p.read_text(encoding="utf-8", errors="replace")
        n_img = text.count("<img")
        n_table = text.count("<table")
        print(f"  {p.name}: {size:,} bytes, {n_img} <img> tag(s), {n_table} <table> tag(s)")
        if size < 2000:
            raise SystemExit(f"{p.name} looks too small ({size} bytes) — report probably incomplete")
        if n_img == 0:
            raise SystemExit(f"{p.name} has NO embedded images — charts did not make it in")

    print("\nOK: charted HTML report produced end-to-end.")
    session.close()


if __name__ == "__main__":
    main()
