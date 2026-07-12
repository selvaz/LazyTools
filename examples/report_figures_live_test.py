"""Live end-to-end test for LazyTools report *figures*, run against DeepSeek.

Exercises the whole "report con grafici" pipeline shipped in lazytoolkit
v0.3.3 — with a real LLM orchestrating and real artifact bytes flowing:

  A. LLM orchestration (DeepSeek cheap tier) — an Agent with the report
     tool provider + a chart-ref helper composes a memo that embeds a chart
     of a real market-data-hub series, renders it to a self-contained HTML,
     and persists it via save_report. Proves: DeepSeek tool-calling ->
     ReportTools.render_memo_html -> ArtifactResolvers -> chart_series ->
     market_data_hub.extract -> base64 <img> -> ReportFiles on disk.

  B. Every artifact scheme (no LLM) — one memo with figures for chart: (real
     datahub PNG), bytes: (inline), file: (local PNG) and regimes: (a real
     synthetic HMM fit rendered into a depot), all embedded into one HTML.
     Proves the resolver registry end to end for the full scheme set.

Setup / running — same convention as the sibling files in this folder
(dependency bootstrap, deepseek.env auto-load, ``-m live`` marker). Beyond
lazybridge[deepseek], this bootstraps the sibling LazyTools[charts],
market-data-hub and LazyStats[regimes] as editable installs::

    pytest -m live -s live_deepseek_tests/test_report_figures_live.py

Skips gracefully if the market-data-hub DuckDB has no data for the probe
symbol, or if hmmlearn/lazystats is unavailable for the regimes leg.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency bootstrap — mirrors test_deepseek_live.py, plus the LazyTools
# report stack (charts extra), market-data-hub and LazyStats[regimes].
# ---------------------------------------------------------------------------

_REQUIRED_PACKAGES: dict[str, str] = {
    "pytest": "pytest>=7.0",
    "pydantic": "pydantic>=2.0.0,<3.0.0",
    "openai": "openai>=1.70.0,<3.0.0",
}

_GITHUB_ROOT = Path(__file__).resolve().parents[2]  # workspace root (LazyTools/examples/.. /..)
_LAZYBRIDGE_ROOT = _GITHUB_ROOT / "LazyBridge"
_LAZYTOOLS_ROOT = _GITHUB_ROOT / "LazyTools"
_MDH_ROOT = _GITHUB_ROOT / "market-data-hub"
_LAZYSTATS_ROOT = _GITHUB_ROOT / "LazyStats"
_DEEPSEEK_ENV_FILE = _GITHUB_ROOT / "deepseek.env"
_HUB_DB = _MDH_ROOT / "market_data.duckdb"


def _pip_install(*args: str) -> None:
    print(f"[report_figures_live] installing: {' '.join(args)}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *args])


def _ensure_dependencies() -> None:
    installed_any = False
    for import_name, pip_spec in _REQUIRED_PACKAGES.items():
        if importlib.util.find_spec(import_name) is None:
            _pip_install(pip_spec)
            installed_any = True
    if importlib.util.find_spec("lazybridge") is None:
        _pip_install("-e", f"{_LAZYBRIDGE_ROOT}[deepseek,test]")
        installed_any = True
    if importlib.util.find_spec("lazytools") is None:
        _pip_install("-e", f"{_LAZYTOOLS_ROOT}[charts]")
        installed_any = True
    if importlib.util.find_spec("market_data_hub") is None:
        _pip_install("-e", str(_MDH_ROOT))
        installed_any = True
    # regimes leg is optional; install if the repo is present
    if importlib.util.find_spec("lazystats") is None and _LAZYSTATS_ROOT.exists():
        _pip_install("-e", f"{_LAZYSTATS_ROOT}[regimes]")
        installed_any = True
    if installed_any:
        importlib.invalidate_caches()


_ensure_dependencies()

import os  # noqa: E402


def _load_deepseek_key() -> None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    if _DEEPSEEK_ENV_FILE.exists():
        key = _DEEPSEEK_ENV_FILE.read_text(encoding="utf-8").strip()
        if key:
            os.environ["DEEPSEEK_API_KEY"] = key


_load_deepseek_key()

# ---------------------------------------------------------------------------
# Now safe to import third-party / lazybridge / lazytools symbols.
# ---------------------------------------------------------------------------
import base64  # noqa: E402

import pytest  # noqa: E402

from lazybridge import Agent, LLMEngine, Session, Tool  # noqa: E402
from lazytools.report import (  # noqa: E402
    ArtifactResolvers,
    FigureBlock,
    Memo,
    ReportFiles,
    ReportTools,
    Section,
    ecosystem_resolvers,
    render_html,
)

MODEL = os.getenv("LB_LIVE_DEEPSEEK_MODEL", "deepseek-v4-flash")

# A valid 1x1 PNG for the bytes:/file: legs.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_PNG = base64.b64decode(_PNG_B64)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("DEEPSEEK_API_KEY"),
        reason="DEEPSEEK_API_KEY not set and no readable deepseek.env next to this folder.",
    ),
]


def _hub_resolvers() -> ArtifactResolvers:
    return ecosystem_resolvers(datahub_db_path=str(_HUB_DB))


def _probe_chart_bytes(symbol: str = "SPY", start: str = "2024-01-01") -> bytes:
    """Resolve a chart ref against the real hub; skip if the series is empty."""
    resolvers = _hub_resolvers()
    try:
        data, mime = resolvers.resolve(f"chart:symbols={symbol}&start={start}&frequency=W")
    except Exception as exc:  # empty window / missing symbol in this DB copy
        pytest.skip(f"hub has no chartable data for {symbol} since {start}: {exc}")
    assert mime == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n"
    return data


# ---------------------------------------------------------------------------
# A. LLM orchestration — DeepSeek composes and renders a report with a figure
# ---------------------------------------------------------------------------


def test_llm_composes_report_with_datahub_chart(tmp_path) -> None:
    if not _HUB_DB.exists():
        pytest.skip(f"market-data-hub DuckDB not found at {_HUB_DB}")
    _probe_chart_bytes()  # ensure the datahub can actually chart SPY (else skip)

    import json

    def chart_ref(symbol: str, start: str) -> str:
        """Return the artifact ref for a weekly price chart of one symbol since `start` (YYYY-MM-DD)."""
        return f"chart:symbols={symbol}&start={start}&frequency=W&title={symbol}"

    sess = Session(db=str(tmp_path / "live.db"), console=True)
    try:
        report = ReportTools(artifacts=_hub_resolvers())  # the shipped tool provider
        agent = Agent(
            engine=LLMEngine(
                MODEL,
                system=(
                    "You build reports by calling tools, one at a time. Never "
                    "invent artifact references — always obtain them from the "
                    "chart_ref tool first, then pass that exact string as a "
                    "figure ref. A memo is a JSON object {title, sections:"
                    "[{title, body, figures:[{ref, caption}]}]}."
                ),
            ),
            tools=[Tool.wrap(chart_ref, name="chart_ref"), *report.as_tools()],
            session=sess,
            name="report_builder",
        )
        agent(
            "Step 1: call chart_ref for symbol SPY, start 2024-01-01 to get a "
            "figure reference. Step 2: call render_memo_html with a memo titled "
            "'SPY Snapshot' whose single section 'Price' has one figure using "
            "that exact reference, captioned 'SPY weekly'. Do both steps."
        )

        # The LLM genuinely drove the tools; assert the rendered HTML with the
        # datahub chart embedded actually flowed through the tool results,
        # regardless of the model's final prose.
        events = list(sess.events.query())
        types = {e["event_type"] for e in events}
        assert "tool_call" in types and "tool_result" in types
        called = json.dumps(events)
        assert "chart_ref" in called, "agent never called chart_ref"
        assert "data:image/png;base64," in called, (
            "no self-contained HTML with an embedded chart appeared in the tool "
            "results — the LLM did not render the memo via render_memo_html"
        )
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# B. Every artifact scheme embedded into one self-contained HTML (no LLM)
# ---------------------------------------------------------------------------


def test_all_artifact_schemes_render(tmp_path) -> None:
    if not _HUB_DB.exists():
        pytest.skip(f"market-data-hub DuckDB not found at {_HUB_DB}")
    _probe_chart_bytes()

    # file: leg — a real PNG on disk
    png_path = tmp_path / "inline.png"
    png_path.write_bytes(_PNG)

    figures = [
        FigureBlock(ref="chart:symbols=SPY&start=2024-01-01&frequency=W", caption="chart scheme"),
        FigureBlock(ref=f"bytes:{_PNG_B64}", caption="bytes scheme"),
        FigureBlock(ref=f"file:{png_path}", caption="file scheme"),
    ]
    resolvers = _hub_resolvers()

    # regimes: leg — a genuine synthetic HMM fit persisted into a depot
    regimes = pytest.importorskip("lazystats.regimes")
    rdb = pytest.importorskip("lazystats.regimes.db")
    np = pytest.importorskip("numpy")
    rdb.init_regime_db(str(tmp_path / "regimes.db"))
    try:
        rng = np.random.RandomState(0)
        n = 160
        states = np.zeros(n, dtype=int)
        states[n // 3 : 2 * n // 3] = 1
        y = np.where(states == 0, rng.normal(0, 0.5, n), rng.normal(0, 3.0, n))
        from lazystats.regimes.tools import _swrite

        _swrite(
            "d",
            {
                "Y": y.reshape(-1, 1),
                "columns": ["SPY"],
                "index": [f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)],
            },
        )
        regimes.fit_regimes(data_key="d", series_names=["SPY"], result_key="r", S_max=2, n_starts=1, random_state=0)
        out = regimes.generate_regime_plots("r")
        plot_key = out["plot_keys"][0]
        resolvers.register("regimes", lambda key, _db=rdb.get_db(): (_db.get_plot(key), "image/png"))
        figures.append(FigureBlock(ref=f"regimes:{plot_key}", caption="regimes scheme"))

        memo = Memo(title="All schemes", sections=[Section(title="Figures", figures=figures)])
        html = render_html(memo, artifacts=resolvers)

        # every figure embedded as its own <img data URI>, plus the inline PNG verbatim
        assert html.count("data:image/png;base64,") == len(figures) == 4
        assert f"data:image/png;base64,{_PNG_B64}" in html  # bytes:/file: legs
        for cap in ("chart scheme", "bytes scheme", "file scheme", "regimes scheme"):
            assert f"<figcaption>{cap}</figcaption>" in html

        out_html = tmp_path / "all_schemes.html"
        out_html.write_text(html, encoding="utf-8")
        assert out_html.stat().st_size > 5000
    finally:
        rdb._DB = None
