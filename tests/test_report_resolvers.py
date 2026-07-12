"""Ecosystem artifact resolvers (regimes/crawler/chart) and datahub charts."""

from __future__ import annotations

import pytest

from lazytools.report import FigureBlock, Memo, Section, render_html
from lazytools.report.charts import parse_chart_spec
from lazytools.report.resolvers import (
    chart_resolver,
    crawler_resolver,
    ecosystem_resolvers,
    regimes_resolver,
)

PNG = b"\x89PNG\r\n\x1a\nfake"


class _StubRegimeDB:
    def get_plot(self, plot_key: str) -> bytes:
        if plot_key != "plot_1":
            raise KeyError(f"Plot '{plot_key}' not found in DB.")
        return PNG


class _StubCrawlerDB:
    def __init__(self, rows):
        self._rows = rows

    def get_artifacts(self, **kwargs):
        assert kwargs.get("include_blob") is True
        return [r for r in self._rows if r.get("content_hash") == kwargs.get("content_hash")]


# --- regimes ------------------------------------------------------------------ #


def test_regimes_resolver_reads_plot_bytes() -> None:
    resolve = regimes_resolver(_StubRegimeDB())
    assert resolve("plot_1") == (PNG, "image/png")
    with pytest.raises(KeyError):
        resolve("missing")


# --- crawler ------------------------------------------------------------------ #


def test_crawler_resolver_reads_blob_and_mime() -> None:
    db = _StubCrawlerDB([{"content_hash": "h1", "blob": PNG, "mime": "image/png"}])
    assert crawler_resolver(db)("h1") == (PNG, "image/png")


def test_crawler_resolver_sniffs_missing_mime() -> None:
    db = _StubCrawlerDB([{"content_hash": "h1", "blob": PNG, "mime": None}])
    assert crawler_resolver(db)("h1")[1] == "image/png"


def test_crawler_resolver_fails_loudly() -> None:
    db = _StubCrawlerDB([{"content_hash": "nb", "blob": None}])
    with pytest.raises(KeyError, match="not found"):
        crawler_resolver(db)("absent")
    with pytest.raises(ValueError, match="no stored bytes"):
        crawler_resolver(db)("nb")


def test_crawler_resolver_skips_bytesless_duplicate() -> None:
    # Same hash on two pages: one row lacks the downloaded blob, one has it.
    db = _StubCrawlerDB([
        {"content_hash": "h1", "blob": None, "mime": "image/png"},
        {"content_hash": "h1", "blob": PNG, "mime": "image/png"},
    ])
    assert crawler_resolver(db)("h1") == (PNG, "image/png")


# --- chart spec ---------------------------------------------------------------- #


def test_parse_chart_spec_full() -> None:
    spec = parse_chart_spec(
        "symbols=SPY,^VIX&start=2020-01-01&end=2026-07-01&frequency=W"
        "&transform=log_return&title=Vol regimes"
    )
    assert spec == {
        "symbols": ["SPY", "^VIX"],
        "start": "2020-01-01",
        "end": "2026-07-01",
        "frequency": "W",
        "transform": "log_return",
        "title": "Vol regimes",
    }


def test_parse_chart_spec_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="must carry 'symbols"):
        parse_chart_spec("start=2020-01-01")
    with pytest.raises(ValueError, match="unknown chart spec field"):
        parse_chart_spec("symbols=SPY&nope=1")


def test_chart_resolver_renders_via_chart_series(monkeypatch) -> None:
    import lazytools.report.charts as charts

    seen = {}

    def _fake_chart_series(symbols, start=None, end=None, *, db_path=None, **kw):
        seen.update({"symbols": symbols, "start": start, "db_path": db_path, **kw})
        return PNG

    monkeypatch.setattr(charts, "chart_series", _fake_chart_series)
    data, mime = chart_resolver(db_path="x.duckdb")("symbols=SPY&start=2020-01-01")
    assert (data, mime) == (PNG, "image/png")
    assert seen["symbols"] == ["SPY"] and seen["start"] == "2020-01-01"
    assert seen["db_path"] == "x.duckdb"


# --- rendering (needs matplotlib + pandas) -------------------------------------- #


def test_render_series_png_produces_png() -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("matplotlib")
    from lazytools.report.charts import render_series_png

    idx = pd.date_range("2026-01-02", periods=30, freq="B")
    df = pd.DataFrame({"SPY": range(30), "^VIX": range(30, 0, -1)}, index=idx)
    png = render_series_png(df, title="two series")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # deterministic: same frame, same bytes
    assert render_series_png(df, title="two series") == png
    with pytest.raises(ValueError, match="empty frame"):
        render_series_png(df.iloc[0:0])


# --- assembly ------------------------------------------------------------------ #


def test_ecosystem_resolvers_schemes() -> None:
    base = ecosystem_resolvers(regimes_db=_StubRegimeDB())
    assert set(base.schemes()) == {"bytes", "chart", "file", "regimes"}
    with_crawler = ecosystem_resolvers(crawler_db=_StubCrawlerDB([]))
    assert "crawler" in with_crawler.schemes()


def test_render_html_with_regimes_figure_end_to_end() -> None:
    import base64

    memo = Memo(
        title="Regimes",
        sections=[Section(title="S", figures=[FigureBlock(ref="regimes:plot_1", caption="SPY bands")])],
    )
    out = render_html(memo, artifacts=ecosystem_resolvers(regimes_db=_StubRegimeDB()))
    assert f'data:image/png;base64,{base64.b64encode(PNG).decode()}' in out
    assert "<figcaption>SPY bands</figcaption>" in out
