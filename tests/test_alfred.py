"""The ALFRED connector against a stub transport, not the real FRED.

The interesting behaviour here is not "does it parse JSON" but the things
that would quietly produce a wrong answer: a vintage silently dropped, a
truncated list that looks complete, the missing-value sentinel read as a
number, and the API key leaking into an error message a model will see.
"""

from __future__ import annotations

import json
import os

import pytest

from lazytools.connectors.alfred import ALFREDClient, ALFREDError, ALFREDTools

ALFRED_TOOLS = {"alfred_vintage", "alfred_vintage_dates"}


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Transport:
    """A stub httpx.Client. Records every query it is handed."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.queries: list[dict] = []

    def get(self, url, params=None):
        self.queries.append(dict(params or {}))
        return self._responses.pop(0) if self._responses else _Response({}, 404)


def _tools(*responses, api_key="test-key"):
    transport = _Transport(*responses)
    client = ALFREDClient(api_key=api_key, transport=transport, min_interval=0)
    return ALFREDTools(client=client), transport


# ------------------------------------------------------------------ surface
def test_the_tool_surface_is_exactly_the_two_reads() -> None:
    tools, _ = _tools()
    assert {t.name for t in tools.as_tools()} == ALFRED_TOOLS


# ------------------------------------------------------------------ vintage
def test_the_vintage_is_sent_on_both_ends_of_the_realtime_window() -> None:
    """Pinning only one end returns a range, not a point in time."""
    tools, transport = _tools(_Response({"observations": []}))
    tools.alfred_vintage("CPIAUCSL", as_of="2020-04-10")
    q = transport.queries[0]
    assert q["realtime_start"] == "2020-04-10"
    assert q["realtime_end"] == "2020-04-10"


def test_a_missing_as_of_is_refused_rather_than_defaulted() -> None:
    """Defaulting to today would answer with revised data and look fine."""
    tools, transport = _tools()
    with pytest.raises(ValueError, match="as_of is required"):
        tools.alfred_vintage("CPIAUCSL", as_of="")
    assert transport.queries == []  # refused before reaching the network


def test_an_empty_series_id_is_refused() -> None:
    tools, _ = _tools()
    with pytest.raises(ValueError, match="series_id is required"):
        tools.alfred_vintage("   ", as_of="2020-04-10")


def test_the_vendors_missing_marker_becomes_none_not_a_number() -> None:
    """FRED writes "." for an unpublished period. Read as a float that is a
    crash; read as 0.0 it is a lie."""
    tools, _ = _tools(_Response({"observations": [
        {"date": "2020-01-01", "value": "258.82", "realtime_start": "2020-04-10"},
        {"date": "2020-02-01", "value": ".", "realtime_start": "2020-04-10"},
    ]}))
    out = tools.alfred_vintage("CPIAUCSL", as_of="2020-04-10")
    assert [o["value"] for o in out["observations"]] == [258.82, None]
    json.dumps(out)  # must stay JSON-safe


def test_a_long_answer_is_truncated_newest_first_and_says_so() -> None:
    rows = [{"date": f"2020-{m:02d}-01", "value": str(m),
             "realtime_start": "2026-01-01"} for m in range(1, 13)] * 40
    tools, _ = _tools(_Response({"observations": rows}))
    out = tools.alfred_vintage("X", as_of="2026-01-01")
    assert out["returned"] == 400
    assert "truncated" in out
    # newest kept: the last source row survives, the first does not
    assert out["observations"][-1]["date"] == rows[-1]["date"]


def test_an_empty_vintage_is_explained_rather_than_left_bare() -> None:
    tools, _ = _tools(_Response({"observations": []}))
    out = tools.alfred_vintage("CPIAUCSL", as_of="2020-04-10")
    assert out["returned"] == 0
    assert "note" in out


# ------------------------------------------------------------- vintage dates
def test_vintage_dates_reports_the_vendor_total_so_truncation_is_visible() -> None:
    """A partial list that looks complete is how a caller concludes a series
    has a short history."""
    tools, _ = _tools(_Response({"vintage_dates": ["2026-08-12", "2026-07-14"],
                                 "count": 668}))
    out = tools.alfred_vintage_dates("CPIAUCSL", limit=2)
    assert out["returned"] == 2
    assert out["total"] == 668
    assert "truncated" in out


def test_a_complete_vintage_list_is_not_flagged_as_truncated() -> None:
    tools, _ = _tools(_Response({"vintage_dates": ["2026-08-12"], "count": 1}))
    out = tools.alfred_vintage_dates("CPIAUCSL")
    assert "truncated" not in out


# -------------------------------------------------------------------- errors
def test_the_api_key_never_reaches_an_error_message() -> None:
    """The message goes to a model and often into a log. The key must not."""
    tools, _ = _tools(
        _Response({"error_message": "Bad Request. Invalid series."}, status=400),
        api_key="SUPER-SECRET-KEY",
    )
    with pytest.raises(ALFREDError) as excinfo:
        tools.alfred_vintage("NOPE", as_of="2020-04-10")
    assert "SUPER-SECRET-KEY" not in str(excinfo.value)


def test_a_vintage_before_the_archive_says_so_instead_of_the_wrong_fix() -> None:
    """FRED's own advice here is to drop realtime_start -- which turns a
    point-in-time read into a revised-data read, the exact mistake this
    connector exists to prevent."""
    tools, _ = _tools(_Response(
        {"error_message": "Bad Request.  The series does not exist in ALFRED "
                          "but may exist in FRED."}, status=400))
    with pytest.raises(ALFREDError) as excinfo:
        tools.alfred_vintage("CPIAUCSL", as_of="1950-01-01")
    message = str(excinfo.value)
    assert "alfred_vintage_dates" in message
    assert "Do not drop the vintage" in message


def test_a_missing_key_is_refused_before_any_request() -> None:
    transport = _Transport()
    client = ALFREDClient(api_key=None, transport=transport, min_interval=0)
    saved = os.environ.pop("FRED_API_KEY", None)
    try:
        with pytest.raises(ALFREDError, match="FRED_API_KEY"):
            client.observations("CPIAUCSL", as_of="2020-04-10")
    finally:
        if saved is not None:
            os.environ["FRED_API_KEY"] = saved
    assert transport.queries == []


def test_the_call_budget_refuses_rather_than_looping() -> None:
    client = ALFREDClient(api_key="k", transport=_Transport(), max_calls=0,
                          min_interval=0)
    with pytest.raises(ALFREDError, match="budget"):
        client.observations("CPIAUCSL", as_of="2020-04-10")


# --------------------------------------------------------------- description
def test_the_first_paragraph_survives_into_the_tool_description() -> None:
    """LazyBridge's SIGNATURE mode exposes only the text before the first blank
    line, so an Args: section below one would never reach the model."""
    from lazybridge import Tool

    tools, _ = _tools()
    tool = Tool.wrap(tools.alfred_vintage, name="alfred_vintage")
    description = tool.definition().description
    assert description
    assert "\n\n" not in description
    assert "as_of" in description
