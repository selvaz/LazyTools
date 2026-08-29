"""Contract tests for the Manifold connector, driven by a stub.

Nothing here reaches the network. The stub records every GET a tool issues
so the request shape is what gets asserted, not just the reply shape. A live
spot-check (querying api.manifold.markets for real) is what confirms the
vendor still answers this way; that is a release check, not something a unit
test can assert.
"""

from __future__ import annotations

import pytest

from lazytools.connectors.manifold import (
    ManifoldBudgetExceeded,
    ManifoldClient,
    ManifoldError,
    ManifoldTools,
)
from lazytools.connectors.manifold.tools import _PROBABILITY_NOTE, MAX_ROWS


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Stub:
    """Stands in for an ``httpx.Client``, recording every GET.

    ``routes`` maps a path (e.g. ``"/markets"``) to the payload returned for
    it; a path not in the map answers 404, the same as the real endpoint
    does for an unknown market id/slug.
    """

    def __init__(self, *, routes: dict[str, object] | None = None, status: int = 200) -> None:
        self.routes = routes or {}
        self.status = status
        self.gets: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.gets.append((url, params or {}))
        for path, payload in self.routes.items():
            if url.endswith(path):
                return _Response(payload, self.status)
        return _Response(None, 404)

    def close(self):  # pragma: no cover - the client only closes what it made
        raise AssertionError("an injected transport must not be closed by the client")


MANIFOLD_TOOL_NAMES = {
    "manifold_list_markets",
    "manifold_search_markets",
    "manifold_get_market",
    "manifold_probability",
    "manifold_recent_bets",
}


def _market_row(
    *,
    id="abc123",
    question="Will it happen?",
    slug="will-it-happen",
    outcome_type="BINARY",
    is_resolved=False,
    probability=0.42,
    answers=None,
    volume=1234.5,
):
    row = {
        "id": id,
        "question": question,
        "slug": slug,
        "url": f"https://manifold.markets/user/{slug}",
        "outcomeType": outcome_type,
        "isResolved": is_resolved,
        "createdTime": 1700000000000,
        "closeTime": 1800000000000,
        "volume": volume,
        "volume24Hours": 67.8,
        "totalLiquidity": 90.1,
        "uniqueBettorCount": 42,
        "probability": probability,
    }
    if answers is not None:
        row["answers"] = answers
    return row


def _tools(stub, **kw):
    return ManifoldTools(client=ManifoldClient(transport=stub, min_interval=0), **kw)


# --------------------------------------------------------------------------- #
# The mounted surface
# --------------------------------------------------------------------------- #
def test_tool_surface_is_exactly_expected() -> None:
    provider = _tools(_Stub())
    assert {t.name for t in provider.as_tools()} == MANIFOLD_TOOL_NAMES


# --------------------------------------------------------------------------- #
# manifold_list_markets: no server-side volume/liquidity sort
# --------------------------------------------------------------------------- #
def test_list_markets_happy_path() -> None:
    stub = _Stub(routes={"/markets": [_market_row(id="m1"), _market_row(id="m2", question="Q2")]})
    tools = _tools(stub)
    out = tools.manifold_list_markets(limit=10)

    assert out["returned"] == 2
    assert [m["id"] for m in out["markets"]] == ["m1", "m2"]
    assert out["markets"][0]["question"] == "Will it happen?"
    assert out["markets"][1]["question"] == "Q2"

    _, params = stub.gets[0]
    assert params["limit"] == 10


def test_list_markets_does_not_reorder_or_filter_the_raw_rows() -> None:
    """The docstring is explicit: this endpoint has no server-side top-by-volume
    ordering, unlike Polymarket's ``order=`` parameter -- rows come back exactly
    as the vendor returned them, low volume first if that's how they arrived,
    and this connector must not silently re-sort or drop any of them."""
    low_then_high = [
        _market_row(id="low-volume", volume=1.0),
        _market_row(id="high-volume", volume=999999.0),
    ]
    stub = _Stub(routes={"/markets": low_then_high})
    tools = _tools(stub)
    out = tools.manifold_list_markets(limit=10)

    assert [m["id"] for m in out["markets"]] == ["low-volume", "high-volume"]
    assert out["markets"][0]["volume"] < out["markets"][1]["volume"]

    # No sort/order parameter is sent -- there is nothing to ask the vendor to
    # rank, unlike Polymarket's polymarket_list_markets(order=...).
    _, params = stub.gets[0]
    assert "order" not in params and "sort" not in params


def test_list_markets_answers_is_none_not_populated() -> None:
    """List rows never carry an ``answers`` array (only manifold_get_market's
    single-market endpoints do) -- confirm the field stays None rather than
    silently defaulting to an empty list that could be mistaken for 'no answers
    exist' instead of 'not fetched here'."""
    stub = _Stub(routes={"/markets": [_market_row(outcome_type="MULTIPLE_CHOICE")]})
    tools = _tools(stub)
    out = tools.manifold_list_markets()
    assert out["markets"][0]["answers"] is None


def test_list_markets_caps_limit_at_max_rows() -> None:
    stub = _Stub(routes={"/markets": []})
    tools = _tools(stub)
    tools.manifold_list_markets(limit=MAX_ROWS + 500)
    _, params = stub.gets[0]
    assert params["limit"] == MAX_ROWS


def test_list_markets_before_pages_past_the_first_result() -> None:
    # Codex PR review finding: without forwarding `before`, every call
    # returned the same newest page and a market older than the first
    # MAX_ROWS results was unreachable.
    stub = _Stub(routes={"/markets": []})
    tools = _tools(stub)
    tools.manifold_list_markets(before="some-market-id")
    _, params = stub.gets[0]
    assert params["before"] == "some-market-id"

    # Omitted (the default) must not send a literal empty string upstream.
    stub2 = _Stub(routes={"/markets": []})
    _tools(stub2).manifold_list_markets()
    _, params2 = stub2.gets[0]
    assert "before" not in params2


# --------------------------------------------------------------------------- #
# manifold_search_markets
# --------------------------------------------------------------------------- #
def test_search_markets_happy_path() -> None:
    stub = _Stub(routes={"/search-markets": [_market_row(id="found1")]})
    tools = _tools(stub)
    out = tools.manifold_search_markets("election", limit=5)

    assert out["term"] == "election"
    assert out["returned"] == 1
    assert out["markets"][0]["id"] == "found1"

    _, params = stub.gets[0]
    assert params["term"] == "election"
    assert params["limit"] == 5


def test_search_markets_rejects_empty_term_before_any_call() -> None:
    stub = _Stub()
    tools = _tools(stub)
    with pytest.raises(ValueError):
        tools.manifold_search_markets("   ")
    assert stub.gets == []


# --------------------------------------------------------------------------- #
# manifold_get_market: exactly one of market_id/slug
# --------------------------------------------------------------------------- #
def test_get_market_requires_exactly_one_of_id_or_slug() -> None:
    stub = _Stub()
    tools = _tools(stub)

    with pytest.raises(ValueError):
        tools.manifold_get_market()
    with pytest.raises(ValueError):
        tools.manifold_get_market(market_id="abc123", slug="will-it-happen")

    assert stub.gets == []


def test_get_market_by_id_happy_path() -> None:
    stub = _Stub(routes={"/market/abc123": _market_row(id="abc123")})
    tools = _tools(stub)
    out = tools.manifold_get_market(market_id="abc123")

    assert out["found"] is True
    assert out["market"]["id"] == "abc123"
    assert out["note"] == _PROBABILITY_NOTE

    url, _ = stub.gets[0]
    assert url.endswith("/market/abc123")


def test_get_market_by_slug_happy_path() -> None:
    stub = _Stub(routes={"/slug/will-it-happen": _market_row(slug="will-it-happen")})
    tools = _tools(stub)
    out = tools.manifold_get_market(slug="will-it-happen")

    assert out["found"] is True
    assert out["market"]["slug"] == "will-it-happen"

    url, _ = stub.gets[0]
    assert url.endswith("/slug/will-it-happen")


def test_get_market_not_found_reports_found_false_without_raising() -> None:
    stub = _Stub(routes={})  # any path answers 404, mirroring an unknown id/slug
    tools = _tools(stub)

    out_by_id = tools.manifold_get_market(market_id="does-not-exist")
    assert out_by_id["found"] is False
    assert "market" not in out_by_id

    out_by_slug = tools.manifold_get_market(slug="does-not-exist")
    assert out_by_slug["found"] is False
    assert "market" not in out_by_slug


def test_get_market_multiple_choice_answers_round_trip() -> None:
    raw_answers = [
        {"text": "Candidate A", "probability": 0.3},
        {"text": "Candidate B", "probability": 0.7},
    ]
    stub = _Stub(
        routes={
            "/market/mc1": _market_row(
                id="mc1", outcome_type="MULTIPLE_CHOICE", probability=None, answers=raw_answers
            )
        }
    )
    tools = _tools(stub)
    out = tools.manifold_get_market(market_id="mc1")

    assert out["found"] is True
    assert out["market"]["answers"] == [
        {"answer": "Candidate A", "probability": 0.3},
        {"answer": "Candidate B", "probability": 0.7},
    ]


# --------------------------------------------------------------------------- #
# manifold_probability
# --------------------------------------------------------------------------- #
def test_probability_requires_market_id_before_any_call() -> None:
    stub = _Stub()
    tools = _tools(stub)
    with pytest.raises(ValueError):
        tools.manifold_probability("")
    assert stub.gets == []


def test_probability_not_found() -> None:
    stub = _Stub(routes={})
    tools = _tools(stub)
    out = tools.manifold_probability("does-not-exist")
    assert out["found"] is False
    assert out["market_id"] == "does-not-exist"


def test_probability_binary_market() -> None:
    stub = _Stub(
        routes={"/market/bin1": _market_row(id="bin1", outcome_type="BINARY", probability=0.73)}
    )
    tools = _tools(stub)
    out = tools.manifold_probability("bin1")

    assert out["found"] is True
    assert out["outcome_type"] == "BINARY"
    assert out["probability"] == 0.73
    assert out["answers"] is None
    assert out["note"] == _PROBABILITY_NOTE


def test_probability_multiple_choice_market_uses_answers() -> None:
    raw_answers = [
        {"text": "Yes-ish", "probability": 0.55},
        {"text": "No-ish", "probability": 0.45},
    ]
    stub = _Stub(
        routes={
            "/market/mc2": _market_row(
                id="mc2", outcome_type="MULTIPLE_CHOICE", probability=None, answers=raw_answers
            )
        }
    )
    tools = _tools(stub)
    out = tools.manifold_probability("mc2")

    assert out["found"] is True
    assert out["outcome_type"] == "MULTIPLE_CHOICE"
    assert out["probability"] is None
    assert out["answers"] == [
        {"answer": "Yes-ish", "probability": 0.55},
        {"answer": "No-ish", "probability": 0.45},
    ]


# --------------------------------------------------------------------------- #
# manifold_recent_bets
# --------------------------------------------------------------------------- #
def test_recent_bets_happy_path_passes_raw_dicts_through_unmodified() -> None:
    raw_bets = [
        {"id": "bet1", "amount": 50, "outcome": "YES", "createdTime": 1700000000000},
        {"id": "bet2", "amount": -25, "outcome": "NO", "createdTime": 1700000100000},
    ]
    stub = _Stub(routes={"/bets": raw_bets})
    tools = _tools(stub)
    out = tools.manifold_recent_bets("abc123", limit=5)

    assert out["market_id"] == "abc123"
    assert out["returned"] == 2
    assert out["bets"] == raw_bets  # untouched, no key renaming/reshaping

    _, params = stub.gets[0]
    assert params["contractId"] == "abc123"
    assert params["limit"] == 5


def test_recent_bets_requires_market_id_before_any_call() -> None:
    stub = _Stub()
    tools = _tools(stub)
    with pytest.raises(ValueError):
        tools.manifold_recent_bets("")
    assert stub.gets == []


def test_recent_bets_raises_when_endpoint_answers_something_unusable() -> None:
    stub = _Stub(routes={"/bets": {"not": "a list"}})
    tools = _tools(stub)
    with pytest.raises(ManifoldError):
        tools.manifold_recent_bets("abc123")


# --------------------------------------------------------------------------- #
# Errors and budget
# --------------------------------------------------------------------------- #
def test_call_budget_stops_a_runaway_loop() -> None:
    stub = _Stub(routes={"/markets": []})
    client = ManifoldClient(transport=stub, min_interval=0, max_calls=2)
    client.list_markets()
    client.list_markets()
    with pytest.raises(ManifoldBudgetExceeded):
        client.list_markets()
