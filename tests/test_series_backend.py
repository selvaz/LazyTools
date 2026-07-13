"""MarketDataHubStatisticsBackend.load_series over monkeypatched hub reads."""

from __future__ import annotations

import pytest

pytest.importorskip("market_data_hub")
pd = pytest.importorskip("pandas")

from lazytools.statistical_analysis.backend import MarketDataHubStatisticsBackend

_DATES = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])


@pytest.fixture()
def hub(monkeypatch):
    from market_data_hub import extract, reader

    calls: dict[str, list] = {"returns": [], "series": [], "factors": []}

    def fake_extract_returns(symbols, start=None, end=None, frequency="W", **kw):
        calls["returns"].append({"symbols": symbols, "frequency": frequency})
        frame = pd.DataFrame({"SPY": [0.01, 0.02, -0.01, 0.03]}, index=_DATES)
        return frame[[s for s in symbols if s in frame.columns]], {"domain": "prices"}

    def fake_extract_series(symbols, start=None, end=None, *, domain, transform,
                            frequency=None, **kw):
        calls["series"].append(
            {"symbols": symbols, "domain": domain, "transform": transform}
        )
        if domain == "macro":
            # starts one day later than the price series, with a NaN hole
            data = {"FEDFUNDS": [None, -0.10, 0.05]}
            index = _DATES[1:]
        else:
            data = {"AAPL": [100.0, 101.0, 99.5, 102.0]}
            index = _DATES
        frame = pd.DataFrame({s: data[s] for s in symbols if s in data}, index=index)
        return frame, {"domain": domain}

    def fake_read_factors(factors=None, factor_set=None, start=None, end=None,
                          wide=True, **kw):
        calls["factors"].append({"factors": factors, "factor_set": factor_set})
        return pd.DataFrame({"Mkt-RF": [0.10, 0.20, -0.05, 0.01]}, index=_DATES)

    monkeypatch.setattr(extract, "extract_returns", fake_extract_returns)
    monkeypatch.setattr(extract, "extract_series", fake_extract_series)
    monkeypatch.setattr(reader, "read_factors", fake_read_factors)
    return calls


def test_load_series_mixed_domains_outer_join(hub) -> None:
    backend = MarketDataHubStatisticsBackend()
    dataset = backend.load_series(
        "ticker:SPY, ticker:AAPL|level, macro:FEDFUNDS|diff, factor:FF5_daily/Mkt-RF",
        frequency="D",
    )
    assert dataset.instruments == [
        "ticker:SPY", "ticker:AAPL", "macro:FEDFUNDS", "factor:FF5_daily/Mkt-RF",
    ]
    assert hub["returns"] == [{"symbols": ["SPY"], "frequency": "D"}]
    assert {"symbols": ["AAPL"], "domain": "prices", "transform": "level"} in hub["series"]
    assert {"symbols": ["FEDFUNDS"], "domain": "macro", "transform": "diff"} in hub["series"]
    assert hub["factors"] == [{"factors": ["Mkt-RF"], "factor_set": "FF5_daily"}]

    # outer join: the macro series starts one day later -> None on day one
    assert dataset.rows[0]["date"] == "2024-01-02"
    assert dataset.rows[0]["macro:FEDFUNDS"] is None
    assert dataset.rows[0]["ticker:SPY"] == pytest.approx(0.01)
    assert dataset.rows[0]["ticker:AAPL"] == pytest.approx(100.0)
    assert dataset.rows[0]["factor:FF5_daily/Mkt-RF"] == pytest.approx(0.10)
    # None (NaN) inside a series is preserved as a missing observation
    assert dataset.rows[1]["macro:FEDFUNDS"] is None
    assert dataset.rows[2]["macro:FEDFUNDS"] == pytest.approx(-0.10)

    meta = dataset.metadata
    assert meta["series"]["macro:FEDFUNDS"] == {"domain": "macro", "transform": "diff"}
    assert meta["series"]["ticker:SPY"] == {"domain": "ticker", "transform": "log_return"}
    assert meta["n_cols"] == 4
    assert meta["date_start"] == "2024-01-02"


def test_load_series_compounds_factor_returns_weekly(hub) -> None:
    backend = MarketDataHubStatisticsBackend()
    dataset = backend.load_series("factor:FF5_daily/Mkt-RF", frequency="W")
    # the four days land in one W-FRI bucket: (1.1 * 1.2 * 0.95 * 1.01) - 1
    assert len(dataset.rows) == 1
    expected = (1 + 0.10) * (1 + 0.20) * (1 - 0.05) * (1 + 0.01) - 1
    assert dataset.rows[0]["factor:FF5_daily/Mkt-RF"] == pytest.approx(expected)


def test_load_series_rejects_bad_input(hub) -> None:
    backend = MarketDataHubStatisticsBackend()
    with pytest.raises(ValueError, match="frequency"):
        backend.load_series("ticker:SPY", frequency="Y")
    with pytest.raises(ValueError, match="at least one"):
        backend.load_series("  ,  ")
    with pytest.raises(ValueError, match="ticker:, factor: and macro:"):
        backend.load_series("macro_panel:USA/real_gdp_growth")


def test_load_series_missing_symbol_becomes_all_none(hub) -> None:
    backend = MarketDataHubStatisticsBackend()
    dataset = backend.load_series("ticker:SPY, ticker:NOPE")
    assert dataset.instruments == ["ticker:SPY", "ticker:NOPE"]
    assert all(row["ticker:NOPE"] is None for row in dataset.rows)
