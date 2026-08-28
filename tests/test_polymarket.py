"""Contract tests for the Polymarket connector, driven by a stub.

Nothing here reaches the network. The stub records every GET a tool issues
so the request shape is what gets asserted, not just the reply shape. A live
spot-check (querying gamma-api.polymarket.com / clob.polymarket.com for
real) is what confirms the vendor still answers this way; that is a release
check, not something a unit test can assert.
"""

from __future__ import annotations

import pytest

from lazytools.connectors.polymarket import (
    PolymarketBudgetExceeded,
    PolymarketClient,
    PolymarketError,
    PolymarketTools,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Stub:
    """Stands in for an ``httpx.Client``, recording every GET.

    ``routes`` maps a path (e.g. ``"/markets"``) to the payload returned for
    it; a path not in the map answers 404, the same as the real endpoints do
    for an unknown resource.
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


POLYMARKET_TOOL_NAMES = {
    "polymarket_list_markets",
    "polymarket_get_market",
    "polymarket_order_book",
    "polymarket_price",
    "polymarket_midpoint",
}


def _market_row(slug="will-it-happen", closed=False):
    return {
        "slug": slug,
        "question": "Will it happen?",
        "active": True,
        "closed": closed,
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.62","0.38"]',
        "clobTokenIds": '["111","222"]',
        "volumeNum": 12345.6,
        "volume24hr": 890.1,
        "liquidityNum": 456.7,
        "endDate": "2027-01-01T00:00:00Z",
    }


def _tools(stub, **kw):
    return PolymarketTools(client=PolymarketClient(transport=stub, min_interval=0), **kw)


# --------------------------------------------------------------------------- #
# The mounted surface
# --------------------------------------------------------------------------- #
def test_tool_surface_is_exactly_expected() -> None:
    provider = _tools(_Stub())
    assert {t.name for t in provider.as_tools()} == POLYMARKET_TOOL_NAMES


# --------------------------------------------------------------------------- #
# Gamma: JSON-string fields, no free-text search
# --------------------------------------------------------------------------- #
def test_list_markets_decodes_json_string_fields() -> None:
    stub = _Stub(routes={"/markets": [_market_row()]})
    tools = _tools(stub)
    out = tools.polymarket_list_markets(limit=10)

    assert out["returned"] == 1
    market = out["markets"][0]
    assert market["outcomes"] == ["Yes", "No"]
    assert market["outcome_prices"] == ["0.62", "0.38"]
    assert market["clob_token_ids"] == ["111", "222"]
    assert market["volume"] == 12345.6

    # closed defaults false, and the endpoint gets no search-like param —
    # narrowing is tag_id/order only, matching the "no free-text search /
    # no tag_slug" gotcha documented on the client and in docs/polymarket.md
    # (tag_slug is real on /events but verified live to be silently ignored
    # on /markets, so it is deliberately not a parameter here at all).
    _, params = stub.gets[0]
    assert params["closed"] == "false"
    assert "q" not in params and "search" not in params and "tag_slug" not in params


def test_list_markets_sends_tag_id_when_given() -> None:
    stub = _Stub(routes={"/markets": []})
    tools = _tools(stub)
    tools.polymarket_list_markets(tag_id=745)
    _, params = stub.gets[0]
    assert params["tag_id"] == 745

    # 0 (the default / "no filter" sentinel) must not be sent as a real id.
    stub2 = _Stub(routes={"/markets": []})
    _tools(stub2).polymarket_list_markets()
    _, params2 = stub2.gets[0]
    assert "tag_id" not in params2


def test_list_markets_defaults_to_active_only_when_open() -> None:
    """The docstring promises 'open/unresolved markets only' for closed=False --
    that requires active=true too, or a disabled/draft market (closed=False,
    active=False) reads as tradable when it is not (Codex PR review finding)."""
    stub = _Stub(routes={"/markets": []})
    _tools(stub).polymarket_list_markets()
    _, params = stub.gets[0]
    assert params["active"] == "true"

    # closed=True has no such promise, and resolved markets are not
    # necessarily active -- forcing the filter there could hide legitimate
    # results, so it is left unconstrained.
    stub2 = _Stub(routes={"/markets": []})
    _tools(stub2).polymarket_list_markets(closed=True)
    _, params2 = stub2.gets[0]
    assert "active" not in params2


def test_list_markets_offset_enables_pagination() -> None:
    """Without offset, anything past the first page was unreachable (Codex
    PR review finding) -- a caller had to already know a market's slug."""
    stub = _Stub(routes={"/markets": []})
    tools = _tools(stub)
    tools.polymarket_list_markets(limit=20, offset=20)
    _, params = stub.gets[0]
    assert params["offset"] == 20


def test_get_market_reports_not_found_without_raising() -> None:
    stub = _Stub(routes={"/markets": []})
    tools = _tools(stub)
    out = tools.polymarket_get_market("no-such-slug")
    assert out["found"] is False
    assert "market" not in out


def test_get_market_finds_exact_slug_match() -> None:
    stub = _Stub(routes={"/markets": [_market_row(slug="election-2028")]})
    tools = _tools(stub)
    out = tools.polymarket_get_market("election-2028")
    assert out["found"] is True
    assert out["market"]["slug"] == "election-2028"


def test_malformed_json_field_degrades_to_empty_list_not_a_crash() -> None:
    row = _market_row()
    row["outcomePrices"] = "not json"
    stub = _Stub(routes={"/markets": [row]})
    tools = _tools(stub)
    out = tools.polymarket_list_markets()
    assert out["markets"][0]["outcome_prices"] == []
    assert out["markets"][0]["outcomes"] == ["Yes", "No"]  # unaffected sibling field


# --------------------------------------------------------------------------- #
# CLOB: book / price / midpoint, keyed by token id
# --------------------------------------------------------------------------- #
def test_order_book_passes_token_id_through() -> None:
    stub = _Stub(routes={"/book": {"bids": [], "asks": []}})
    tools = _tools(stub)
    out = tools.polymarket_order_book("111")
    assert out["book"] == {"bids": [], "asks": []}
    _, params = stub.gets[0]
    assert params["token_id"] == "111"


def test_price_rejects_unknown_side() -> None:
    tools = _tools(_Stub())
    with pytest.raises(ValueError, match="side must be"):
        tools.polymarket_price("111", side="hold")


def test_price_and_midpoint() -> None:
    stub = _Stub(routes={"/price": {"price": "0.63"}, "/midpoint": {"mid": "0.62"}})
    tools = _tools(stub)
    out = tools.polymarket_price("111", side="buy")
    assert out["price"] == {"price": "0.63"}
    # The runtime note (mirroring the docstring) is the load-bearing part:
    # verified live against the vendor 2026-08-28 that side='buy' returns
    # the best BID, not "the price you'd pay to buy" -- a caller that
    # missed the docstring should still see this in the reply.
    assert "best bid" in out["note"] and "best ask" in out["note"]
    assert tools.polymarket_midpoint("111")["midpoint"] == {"mid": "0.62"}


def test_price_side_is_sent_through_unchanged() -> None:
    stub = _Stub(routes={"/price": {"price": "0.5"}})
    tools = _tools(stub)
    tools.polymarket_price("111", side="sell")
    _, params = stub.gets[0]
    assert params["side"] == "sell"


def test_missing_token_id_raises_before_any_call() -> None:
    stub = _Stub()
    tools = _tools(stub)
    with pytest.raises(ValueError):
        tools.polymarket_order_book("")
    assert stub.gets == []


# --------------------------------------------------------------------------- #
# Errors and budget
# --------------------------------------------------------------------------- #
def test_book_raises_when_endpoint_answers_nothing_usable() -> None:
    stub = _Stub(status=404)
    tools = _tools(stub)
    with pytest.raises(PolymarketError):
        tools.polymarket_order_book("does-not-exist")


def test_call_budget_stops_a_runaway_loop() -> None:
    stub = _Stub(routes={"/midpoint": {"mid": "0.5"}})
    client = PolymarketClient(transport=stub, min_interval=0, max_calls=2)
    client.midpoint("111")
    client.midpoint("111")
    with pytest.raises(PolymarketBudgetExceeded):
        client.midpoint("111")
