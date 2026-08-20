"""The earnings connector against a real hub database, not a mock.

Every method opens the hub read-only and forwards to one hub function, so the
thing worth testing is exactly that seam: that the window, the filters and the
grouping arrive intact, and that an unknown id answers empty instead of raising.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("market_data_hub")

from market_data_hub.db import connection as cx
from market_data_hub.earnings_calendar import (
    EarningsObservation,
    ingest_observations,
)

from lazytools.connectors.earnings_calendar import EarningsCalendarTools


def _oss(symbol: str, giorno: str, **kw) -> EarningsObservation:
    base = dict(exchange="XETR", source="tradingview", status="estimated",
                country="Germany", sector="Electronic Technology",
                industry="Semiconductors", market_cap=2.0e10, currency="EUR")
    base.update(kw)
    return EarningsObservation(symbol=symbol,
                               release_ts_utc=datetime.fromisoformat(giorno), **base)


@pytest.fixture()
def tools(tmp_path):
    percorso = tmp_path / "hub.duckdb"
    con = cx.get_conn(str(percorso))
    ingest_observations(con, [
        _oss("AAA", "2026-08-25T06:30:00"),
        _oss("BBB", "2026-08-27T06:30:00", market_cap=5.0e9),
        _oss("CCC", "2026-09-10T06:30:00", country="Japan", exchange="TSE",
             status="occurred", eps_estimate=2.0, eps_actual=2.2),
    ])
    con.close()
    return EarningsCalendarTools(db_path=str(percorso))


def test_the_week_window_excludes_its_end(tools) -> None:
    simboli = {e["symbol"] for e in tools.earnings_week("2026-08-25", "2026-08-27")}
    assert simboli == {"AAA"}


def test_the_biggest_company_comes_first(tools) -> None:
    righe = tools.earnings_week("2026-08-01", "2026-10-01")
    assert [r["symbol"] for r in righe][:2] == ["AAA", "CCC"]


def test_a_market_cap_floor_drops_the_small_ones(tools) -> None:
    righe = tools.earnings_week("2026-08-01", "2026-10-01", min_market_cap=1e10)
    assert "BBB" not in {r["symbol"] for r in righe}


def test_a_region_filter_reaches_the_hub(tools) -> None:
    righe = tools.earnings_week("2026-08-01", "2026-10-01", region="japan")
    assert [r["symbol"] for r in righe] == ["CCC"]


def test_a_day_is_that_utc_day_alone(tools) -> None:
    assert [e["symbol"] for e in tools.earnings_for_day("2026-08-25")] == ["AAA"]
    assert tools.earnings_for_day("2026-08-26") == []


def test_aggregation_counts_the_releases_it_does_not_list(tools) -> None:
    per_paese = {r["bucket"]: r for r in
                 tools.earnings_aggregate("2026-08-01", "2026-10-01", by="country")}
    assert per_paese["Germany"]["n"] == 2
    assert per_paese["Japan"]["occurred"] == 1


def test_aggregation_refuses_a_grouping_that_does_not_exist(tools) -> None:
    with pytest.raises(ValueError):
        tools.earnings_aggregate("2026-08-01", "2026-10-01", by="ceo_surname")


def test_an_event_carries_the_versions_that_produced_it(tools) -> None:
    eid = tools.earnings_week("2026-09-01", "2026-10-01")[0]["event_id"]
    evento = tools.earnings_event(eid)
    assert evento["symbol"] == "CCC"
    assert [v["source"] for v in evento["versions"]] == ["tradingview"]


def test_an_unknown_event_answers_empty_rather_than_raising(tools) -> None:
    assert tools.earnings_event("nonesiste") == {}


def test_the_vocabulary_reports_what_is_stored(tools) -> None:
    vocabolario = tools.earnings_vocabulary()
    assert {v["value"] for v in vocabolario["regions"]} == {"europe", "japan"}
    assert vocabolario["stored_from"].startswith("2026-08-25")
