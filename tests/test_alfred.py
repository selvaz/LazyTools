"""Tests for the ALFRED vintage connector.

These tests never depend on the installed ``market_data_hub`` package's real
``read_alfred_vintage`` behavior:

* the happy-path / filter-translation / empty-result tests install a fake
  ``market_data_hub.reader`` module via monkeypatch (the same
  ``sys.modules`` technique ``tests/test_treasury_fiscal.py`` and
  ``tests/test_cftc_cot.py`` use for their own hub functions);
* the ``series_id`` validation test is deliberately run against the real,
  *unpatched* package -- if ``alfred_vintage``'s guard clause ever moved to
  after the ``from market_data_hub.reader import read_alfred_vintage`` line,
  a broken/absent reader would surface as an unrelated error instead of the
  expected ``ValueError``, and this test would catch the regression.
"""

from __future__ import annotations

import sys
import types

import pytest

pytest.importorskip("market_data_hub")

import pandas as pd  # must follow the importorskip above

from lazytools.connectors.alfred import ALFREDTools


def _install_fake_reader(monkeypatch, **fakes):
    import market_data_hub

    fake_reader = types.SimpleNamespace(**fakes)
    monkeypatch.setattr(market_data_hub, "reader", fake_reader, raising=False)
    monkeypatch.setitem(sys.modules, "market_data_hub.reader", fake_reader)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["date", "series_id", "value", "as_of", "source"])


def test_the_tool_surface_is_exactly_one_tool() -> None:
    provider = ALFREDTools()
    assert {t.name for t in provider.as_tools()} == {"alfred_vintage"}


def test_a_revision_round_trips_as_two_plain_dict_rows(monkeypatch) -> None:
    # A real CPI revision, live-verified against FRED: the same observation
    # date (2020-03-01) reported at two different vintages, with the
    # earlier-vintage figure since revised upward -- exactly the kind of
    # thing a backtest must not blend into one "the" value for that date.
    frame = pd.DataFrame([
        {"date": "2020-03-01", "series_id": "CPIAUCSL", "value": 257.953,
         "as_of": "2020-04-10", "source": "fred"},
        {"date": "2020-03-01", "series_id": "CPIAUCSL", "value": 257.989,
         "as_of": "2021-02-08", "source": "fred"},
    ])

    def fake_read_alfred_vintage(series_id, *, date=None, as_of=None, db_path=None):
        return frame

    _install_fake_reader(monkeypatch, read_alfred_vintage=fake_read_alfred_vintage)

    result = ALFREDTools().alfred_vintage("CPIAUCSL")

    assert result["returned"] == 2
    observations = result["observations"]
    assert isinstance(observations, list)
    assert all(isinstance(row, dict) and not isinstance(row, pd.DataFrame) for row in observations)
    by_as_of = {row["as_of"]: row["value"] for row in observations}
    assert by_as_of == {"2020-04-10": 257.953, "2021-02-08": 257.989}
    assert all(row["date"] == "2020-03-01" for row in observations)
    assert "note" not in result


def test_real_hub_dtypes_survive_json_serialization(monkeypatch) -> None:
    # The hub's actual read_alfred_vintage returns real datetime64 `date`/
    # `as_of` columns and NaN for a legitimately-absent `value` -- neither
    # is JSON-safe via a bare `to_dict(orient="records")`. This is the exact
    # shape that regressed on the sibling treasury_fiscal.py/cftc_cot.py
    # connectors, so it must be exercised here too, not just with
    # pre-formatted string fixtures.
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2020-03-01", "2020-04-01"]),
        "series_id": ["CPIAUCSL", "CPIAUCSL"],
        "value": [257.953, float("nan")],
        "as_of": pd.to_datetime(["2020-04-10", "2020-04-10"]),
        "source": ["fred", "fred"],
    })

    def fake_read_alfred_vintage(series_id, *, date=None, as_of=None, db_path=None):
        return frame

    _install_fake_reader(monkeypatch, read_alfred_vintage=fake_read_alfred_vintage)

    result = ALFREDTools().alfred_vintage("CPIAUCSL")

    import json
    json.dumps(result)  # must not raise -- Timestamp/NaN are not JSON-safe

    observations = result["observations"]
    assert observations[0]["date"] == "2020-03-01"
    assert observations[0]["as_of"] == "2020-04-10"
    assert observations[0]["value"] == 257.953
    assert observations[1]["value"] is None


def test_series_id_is_stripped_consistently(monkeypatch) -> None:
    # The hub call and the returned "series_id" field must agree on the
    # stripped value -- a caller passing " CPIAUCSL " should neither query
    # nor be told about a series literally named " CPIAUCSL ".
    calls: list[str] = []

    def fake_read_alfred_vintage(series_id, *, date=None, as_of=None, db_path=None):
        calls.append(series_id)
        return _empty_frame()

    _install_fake_reader(monkeypatch, read_alfred_vintage=fake_read_alfred_vintage)

    result = ALFREDTools().alfred_vintage("  CPIAUCSL  ")

    assert calls == ["CPIAUCSL"]
    assert result["series_id"] == "CPIAUCSL"


def test_an_empty_series_id_is_rejected_before_any_hub_call() -> None:
    # No monkeypatch here on purpose -- see module docstring.
    with pytest.raises(ValueError):
        ALFREDTools().alfred_vintage("")


def test_a_whitespace_only_series_id_is_also_rejected() -> None:
    with pytest.raises(ValueError):
        ALFREDTools().alfred_vintage("   ")


@pytest.mark.parametrize(
    ("date", "as_of", "expected_date", "expected_as_of"),
    [
        pytest.param("", "", None, None, id="neither-given"),
        pytest.param("2020-03-01", "", "2020-03-01", None, id="only-date"),
        pytest.param("", "2020-05-12", None, "2020-05-12", id="only-as_of"),
        pytest.param("2020-03-01", "2020-05-12", "2020-03-01", "2020-05-12", id="both-given"),
    ],
)
def test_empty_string_filters_translate_to_none(
    monkeypatch, date, as_of, expected_date, expected_as_of
) -> None:
    calls: list[dict] = []

    def fake_read_alfred_vintage(series_id, *, date=None, as_of=None, db_path=None):
        calls.append({"series_id": series_id, "date": date, "as_of": as_of, "db_path": db_path})
        return _empty_frame()

    _install_fake_reader(monkeypatch, read_alfred_vintage=fake_read_alfred_vintage)

    ALFREDTools(db_path="hub.duckdb").alfred_vintage("CPIAUCSL", date=date, as_of=as_of)

    assert len(calls) == 1
    assert calls[0]["series_id"] == "CPIAUCSL"
    assert calls[0]["date"] == expected_date
    assert calls[0]["as_of"] == expected_as_of
    assert calls[0]["db_path"] == "hub.duckdb"


def test_an_empty_result_carries_a_not_yet_backfilled_note(monkeypatch) -> None:
    def fake_read_alfred_vintage(series_id, *, date=None, as_of=None, db_path=None):
        return _empty_frame()

    _install_fake_reader(monkeypatch, read_alfred_vintage=fake_read_alfred_vintage)

    result = ALFREDTools().alfred_vintage("NOTBACKFILLEDYET")

    # This is a deliberate distinction the implementation draws: an empty
    # result for a real series most likely means the hub ingestion job has
    # not backfilled it yet, not that the series genuinely has no history.
    # Pin it so a future refactor can't silently drop the note.
    assert result["returned"] == 0
    assert result["observations"] == []
    assert "note" in result
    assert "not" in result["note"]
    assert "backfilled" in result["note"]
    assert "alfred_vintage_observations" in result["note"]


def test_a_filtered_empty_result_carries_a_different_note(monkeypatch) -> None:
    # A miss with date/as_of set is ambiguous in a way an unfiltered miss is
    # not: it could mean this exact vintage isn't stored, OR that the series
    # isn't backfilled at all. The note must not claim the narrower "not
    # backfilled" explanation as the only one.
    def fake_read_alfred_vintage(series_id, *, date=None, as_of=None, db_path=None):
        return _empty_frame()

    _install_fake_reader(monkeypatch, read_alfred_vintage=fake_read_alfred_vintage)

    result = ALFREDTools().alfred_vintage("CPIAUCSL", date="1999-01-01")

    assert "note" in result
    assert "may mean" in result["note"]


def test_the_docstrings_first_paragraph_explains_alfred_and_the_filters() -> None:
    # LazyBridge's SIGNATURE-mode schema builder only exposes the docstring
    # text up to the first blank line as the tool's LLM-visible description
    # -- an Args: section added below a blank line would never reach the
    # model. This asserts against the actual built description, not the raw
    # docstring, so a regression there is caught even if the source
    # docstring still reads fine to a human.
    from lazybridge import Tool

    tool = Tool.wrap(ALFREDTools().alfred_vintage, name="alfred_vintage")
    description = tool.definition().description

    assert description
    assert "\n\n" not in description, "description leaked past the first paragraph"

    assert "ALFRED" in description
    assert "historically published" in description

    # the four date/as_of combinations, each named in the first paragraph
    assert "only date" in description
    assert "only as_of" in description
    assert "both" in description
    assert "neither" in description
