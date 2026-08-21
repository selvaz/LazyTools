"""Contract tests for the live TradingView connector, driven by a stub.

Nothing here reaches the network. The stub records the exact body each tool
posts, which is the point: the value of a bounded surface is that the request
is predictable, so the request is what gets asserted.

The one thing a stub cannot check is whether the vendor still answers the way
it did — that is what the ``non_null`` counts in the replies are for at
runtime, and what a live spot-check is for before a release.
"""

from __future__ import annotations

import pytest

from lazytools.connectors.tradingview import (
    BREADTH_METRICS,
    FIELDS,
    SCREENS,
    ScreenerBudgetExceeded,
    ScreenerClient,
    ScreenerError,
    TradingViewTools,
)
from lazytools.connectors.tradingview.catalog import BUNDLES, WITHHELD, tv_columns


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Stub:
    """Stands in for an ``httpx.Client``, recording every call."""

    def __init__(self, *, rows=None, total=0, status=200, metainfo=None):
        self.rows = rows or []
        self.total = total
        self.status = status
        self.metainfo_payload = metainfo or {
            "fields": [{"n": "sector", "t": "text", "r": ["Energy Minerals", "Finance"]}]
        }
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def post(self, url, json=None):
        self.posts.append((url, json))
        return _Response({"totalCount": self.total, "data": self.rows}, self.status)

    def get(self, url):
        self.gets.append(url)
        return _Response(self.metainfo_payload, self.status)

    def close(self):  # pragma: no cover - the client only closes what it made
        raise AssertionError("an injected transport must not be closed by the client")


#: The mounted surface, asserted here as well as in the MCP contract test: this
#: file is where a tool would be added, so this is where a forgotten one shows.
TRADINGVIEW_TOOL_NAMES = {
    "tradingview_vocabulary",
    "tradingview_fields",
    "tradingview_resolve",
    "tradingview_quote",
    "tradingview_screen",
    "tradingview_breadth",
}


def _tools(stub, **kw):
    return TradingViewTools(client=ScreenerClient(transport=stub, min_interval=0), **kw)


def _row(ticker, values):
    return {"s": ticker, "d": list(values)}


# --------------------------------------------------------------------------- #
# The catalogue is the surface: it has to be internally consistent
# --------------------------------------------------------------------------- #
def test_every_bundle_field_exists() -> None:
    for bundle, names in BUNDLES.items():
        unknown = [n for n in names if n not in FIELDS]
        assert not unknown, f"bundle {bundle} names unknown fields {unknown}"


def test_every_screen_is_coherent() -> None:
    for name, spec in SCREENS.items():
        assert spec.columns in BUNDLES, f"screen {name} sorts into an unknown bundle"
        assert spec.sort_by in FIELDS, f"screen {name} sorts by an unknown field"
        assert spec.sort_by in BUNDLES[spec.columns], (
            f"screen {name} sorts by {spec.sort_by}, which its own columns do not "
            f"return -- the reader could not see the ordering key"
        )


def test_withheld_fields_are_not_reachable() -> None:
    for name in WITHHELD:
        assert name not in FIELDS
        with pytest.raises(ValueError, match="deliberately not exposed"):
            tv_columns([name])


def test_unknown_field_raises_rather_than_passing_through() -> None:
    # The endpoint answers an unknown column with null, which reads exactly
    # like a real missing value. Failing loudly here is the whole point.
    with pytest.raises(ValueError, match="unknown field"):
        tv_columns(["ebitda_margin_maybe"])


def test_timeframe_suffix_applies_only_where_it_means_something() -> None:
    assert tv_columns(["rsi"], "1W") == ["RSI|1W"]
    assert tv_columns(["rsi"], "1D") == ["RSI"]
    assert tv_columns(["aum"], "1W") == ["aum"]  # not timeframed: no suffix
    with pytest.raises(ValueError, match="unknown timeframe"):
        tv_columns(["rsi"], "1Y")


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
def test_breadth_counts_and_never_carries_rows() -> None:
    stub = _Stub(total=4000)
    tools = _tools(stub)
    out = tools.tradingview_breadth(universe="us_cap1b", metrics="above_sma200")

    assert out["n_universe"] == 4000
    assert out["metrics"]["above_sma200"]["ratio_pct"] == 100.0
    # One call for the universe, then per metric one for the observable subset
    # and one for the hits -- each asking for a single row: breadth reads
    # totalCount, never data.
    assert len(stub.posts) == 3
    for _, body in stub.posts:
        assert body["range"] == [0, 1]
        assert body["columns"] == ["name"]
    # The filter that produced the number travels with it.
    assert out["metrics"]["above_sma200"]["filter"] == BREADTH_METRICS["above_sma200"].clause
    assert out["universe_filter"]


def test_an_instrument_that_cannot_answer_is_not_counted_as_a_no() -> None:
    """A stock listed last month has no 200-day average.

    Counting it in the denominator files it under "not above the average",
    which is not what it is: it is unmeasurable. The stub answers 4000 for the
    universe and 3000 thereafter, so the ratio must be over 3000.
    """
    class _Tiered:
        def __init__(self):
            self.totals = [4000, 3000, 1500]
            self.i = -1

        def post(self, url, json=None):
            self.i += 1
            return _Response({"totalCount": self.totals[min(self.i, 2)], "data": []})

        def get(self, url):  # pragma: no cover - not reached here
            raise AssertionError

    tools = TradingViewTools(client=ScreenerClient(transport=_Tiered(), min_interval=0))
    m = tools.tradingview_breadth(metrics="above_sma200")["metrics"]["above_sma200"]
    assert m["n_observable"] == 3000
    assert m["n_universe"] == 4000
    assert m["unmeasurable"] == 1000
    assert m["ratio_pct"] == 50.0  # 1500 / 3000, not 1500 / 4000


def test_a_metric_nothing_can_answer_says_so_instead_of_reporting_zero() -> None:
    """A renamed vendor field would otherwise publish a confident 0 %."""
    stub = _Stub(total=0)
    m = _tools(stub).tradingview_breadth(metrics="above_sma200")["metrics"]["above_sma200"]
    assert m["ratio_pct"] is None
    assert "NOT '0 % of the market'" in m["warning"]


def test_breadth_rejects_a_guessed_sector_before_spending_a_call() -> None:
    stub = _Stub(total=10)
    tools = _tools(stub)
    with pytest.raises(ValueError, match="TradingView's own spelling"):
        tools.tradingview_breadth(sector="Technology")
    assert stub.posts == []  # only metainfo was fetched
    assert stub.gets


def test_breadth_accepts_the_vendor_spelling() -> None:
    stub = _Stub(total=10)
    tools = _tools(stub)
    out = tools.tradingview_breadth(metrics="up_today", sector="Energy Minerals")
    assert out["sector"] == "Energy Minerals"
    assert {"left": "sector", "operation": "equal", "right": "Energy Minerals"} in out["universe_filter"]


def test_screen_filter_is_fixed_and_returned() -> None:
    stub = _Stub(rows=[_row("AMEX:SPY", ["SPY", "SPDR", "AMEX", 1.0, 2.0, 3, 4.0, "USD"])], total=1)
    tools = _tools(stub)
    out = tools.tradingview_screen("largest_market_cap", limit=1)

    _, body = stub.posts[0]
    assert body["filter"] == list(SCREENS["largest_market_cap"].filter)
    assert body["sort"] == {"sortBy": "market_cap_basic", "sortOrder": "desc"}
    assert out["filter_applied"] == list(SCREENS["largest_market_cap"].filter)
    assert out["matched_total"] == 1


def test_screen_limit_is_capped() -> None:
    stub = _Stub(rows=[], total=0)
    tools = _tools(stub)
    tools.tradingview_screen("largest_market_cap", limit=10_000)
    _, body = stub.posts[0]
    assert body["range"] == [0, 100]


def test_screen_rejects_a_market_it_was_not_defined_for() -> None:
    tools = _tools(_Stub())
    with pytest.raises(ValueError, match="is defined for"):
        tools.tradingview_screen("etf_largest_aum", market="crypto")


def test_unknown_screen_points_at_the_vocabulary() -> None:
    tools = _tools(_Stub())
    with pytest.raises(ValueError, match="tradingview_vocabulary"):
        tools.tradingview_screen("biggest_movers_maybe")


# --------------------------------------------------------------------------- #
# Resolution: the failure this connector exists to prevent
# --------------------------------------------------------------------------- #
def test_resolution_returns_the_venue_the_vendor_actually_uses() -> None:
    stub = _Stub(rows=[
        _row("NASDAQ:EMB", ["EMB", "iShares EM Bond", "NASDAQ", "United States", "fund", True]),
        _row("CBOE:INDA", ["INDA", "iShares MSCI India", "CBOE", "United States", "fund", True]),
    ], total=2)
    tools = _tools(stub)
    out = tools.tradingview_resolve("EMB,INDA")
    assert out["resolved"] == {"EMB": "NASDAQ:EMB", "INDA": "CBOE:INDA"}
    assert out["not_found"] == []


def test_a_collision_is_reported_not_silently_picked() -> None:
    # Two real instruments share the ticker 7203 on different venues and
    # neither is flagged primary: choosing one would be inventing an answer.
    stub = _Stub(rows=[
        _row("TSE:7203", ["7203", "Toyota Motor Corp.", "TSE", "Japan", "stock", False]),
        _row("TADAWUL:7203", ["7203", "Elm Company", "TADAWUL", "Saudi Arabia", "stock", False]),
    ], total=2)
    tools = _tools(stub)
    out = tools.tradingview_resolve("7203", market="global")
    assert "7203" not in out["resolved"]
    assert len(out["ambiguous"]["7203"]) == 2


def test_a_cross_listing_resolves_to_the_primary_and_still_reports_the_others() -> None:
    stub = _Stub(rows=[
        _row("NYSE:ABC", ["ABC", "Abc Inc", "NYSE", "United States", "stock", True]),
        _row("OTC:ABC1", ["ABC", "Abc Inc", "OTC", "United States", "stock", False]),
    ], total=2)
    tools = _tools(stub)
    out = tools.tradingview_resolve("ABC")
    assert out["resolved"] == {"ABC": "NYSE:ABC"}
    assert out["also_listed"]["ABC"][0]["tv_ticker"] == "OTC:ABC1"


def test_one_primary_is_not_enough_when_the_issuers_differ() -> None:
    """`is_primary` describes ONE listing, not a link between listings.

    Toyota is primary on TSE; Elm is a different company that happens to trade
    under 7203 in Riyadh. Picking the primary would answer with Toyota and file
    Elm under "also listed" as though it were another venue for the same share.
    """
    stub = _Stub(rows=[
        _row("TSE:7203", ["7203", "Toyota Motor Corp.", "TSE", "Japan", "stock", True]),
        _row("TADAWUL:7203", ["7203", "Elm Company", "TADAWUL", "Saudi Arabia", "stock", False]),
    ], total=2)
    out = _tools(stub).tradingview_resolve("7203", market="global")
    assert "7203" not in out["resolved"]
    assert {c["tv_ticker"] for c in out["ambiguous"]["7203"]} == {"TSE:7203", "TADAWUL:7203"}


def test_a_truncated_resolution_does_not_claim_a_symbol_does_not_exist() -> None:
    """`not_found` must never mean "past the last row we read"."""
    stub = _Stub(rows=[
        _row("NYSE:AAA", ["AAA", "Aaa Inc", "NYSE", "United States", "stock", True]),
    ], total=900)
    out = _tools(stub).tradingview_resolve("AAA,BBB")
    assert out["truncated"] is True
    assert out["not_found"] == [], "never having looked is not 'not found'"
    assert "BBB" in out["not_seen"]
    assert "not_seen" in out["warning"]


def test_a_breadth_universe_is_counted_on_its_own_market() -> None:
    """A provider pointed at another market must not relabel world numbers.

    The filters say "a stock above a capitalisation floor" and nothing about
    being American: what makes us_cap1b American is the endpoint it is counted
    on, so the universe carries its market and the provider's cannot override it.
    """
    stub = _Stub(total=10)
    tools = _tools(stub, market="global")
    out = tools.tradingview_breadth(universe="us_cap1b", metrics="up_today")
    assert out["market"] == "america"
    for url, _ in stub.posts:
        assert "/america/scan" in url


def test_an_unreadable_sector_list_fails_closed() -> None:
    """Accepting an unchecked sector would turn a spelling mistake into a zero."""
    stub = _Stub(total=10, metainfo={"fields": []})
    with pytest.raises(ScreenerError, match="could not be read"):
        _tools(stub).tradingview_breadth(sector="Energy Minerals")


def test_missing_symbols_are_named() -> None:
    stub = _Stub(rows=[], total=0)
    tools = _tools(stub)
    assert tools.tradingview_resolve("NOPE")["not_found"] == ["NOPE"]


# --------------------------------------------------------------------------- #
# Quotes: units and the renamed-field failure
# --------------------------------------------------------------------------- #
def test_quote_carries_units_and_uses_qualified_tickers_directly() -> None:
    names = BUNDLES["fund"]
    stub = _Stub(rows=[_row("AMEX:AGG", [None] * len(names))], total=1)
    tools = _tools(stub)
    out = tools.tradingview_quote("AMEX:AGG", bundle="fund")

    _, body = stub.posts[0]
    assert body["symbols"] == {"tickers": ["AMEX:AGG"]}
    assert len(stub.posts) == 1, "a qualified ticker must not cost a resolution call"
    assert out["units"]["expense_ratio"] == "pct"
    assert out["units"]["flow_1m"] == "usd"


def test_quote_reports_an_all_null_column_so_a_vendor_rename_is_visible() -> None:
    names = BUNDLES["fund"]
    values = [None] * len(names)
    values[names.index("symbol")] = "AGG"
    values[names.index("aum")] = 1.0
    stub = _Stub(rows=[_row("AMEX:AGG", values)], total=1)
    out = _tools(stub).tradingview_quote("AMEX:AGG", bundle="fund")
    assert out["non_null"]["aum"] == 1
    assert out["non_null"]["flow_1m"] == 0


def test_quote_rejects_an_oversized_request() -> None:
    tools = _tools(_Stub())
    with pytest.raises(ValueError, match="at most"):
        tools.tradingview_quote(",".join(f"S{i}" for i in range(200)))


def test_percentage_noise_is_rounded_but_money_is_not() -> None:
    names = BUNDLES["fund"]
    values = [None] * len(names)
    values[names.index("expense_ratio")] = 0.35000000000000003
    values[names.index("aum")] = 41784349376.1284
    stub = _Stub(rows=[_row("AMEX:XLE", values)], total=1)
    row = _tools(stub).tradingview_quote("AMEX:XLE", bundle="fund")["rows"][0]
    assert row["expense_ratio"] == 0.35
    assert row["aum"] == 41784349376.1284


def test_dates_keep_the_hour_the_vendor_gave() -> None:
    """A results date at 20:00Z is after the US close -- a bare date loses that."""
    from lazytools.connectors.tradingview.tools import _decode

    assert _decode("earnings_next_date", 1787774400) == "2026-08-26T20:00:00+00:00"
    assert _decode("earnings_next_date", None) is None
    assert _decode("earnings_next_date", "not a date") is None


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def test_the_call_budget_stops_a_loop() -> None:
    stub = _Stub(total=1)
    tools = TradingViewTools(client=ScreenerClient(transport=stub, max_calls=2, min_interval=0))
    with pytest.raises(ScreenerBudgetExceeded):
        for _ in range(5):
            tools.tradingview_breadth(metrics="up_today")


def test_a_rate_limit_is_not_retried() -> None:
    stub = _Stub(total=0, status=429)
    tools = _tools(stub)
    with pytest.raises(ScreenerError, match="do not loop"):
        tools.tradingview_breadth(metrics="up_today")
    assert len(stub.posts) == 1


def test_an_unknown_market_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown market"):
        TradingViewTools(market="milano")


def test_vocabulary_is_free_of_network_until_enumerations_are_asked_for() -> None:
    stub = _Stub()
    tools = _tools(stub)
    out = tools.tradingview_vocabulary()
    assert stub.posts == [] and stub.gets == []
    assert set(out["screens"]) == set(SCREENS)
    tools.tradingview_vocabulary(section="enumerations")
    assert stub.gets, "the enumerations come from the endpoint, not a hard-coded copy"


def test_every_reply_is_stamped() -> None:
    stub = _Stub(total=1)
    tools = _tools(stub)
    for reply in (
        tools.tradingview_vocabulary(),
        tools.tradingview_fields(search="flow"),
        tools.tradingview_breadth(metrics="up_today"),
    ):
        assert reply["as_of"].endswith("+00:00")
        assert "TradingView" in reply["source"]
        assert "calls_made" in reply


def test_the_caveats_survive_into_what_the_model_actually_sees() -> None:
    """The tool description is the docstring's FIRST PARAGRAPH, nothing more.

    An MCP client gets no system prompt from us, so a warning written below the
    blank line reaches nobody. This test exists because that is exactly where
    the warnings were first written.
    """
    tools = {t.name: t for t in _tools(_Stub()).as_tools()}
    must_say = {
        "tradingview_vocabulary": "closed vocabulary",
        "tradingview_resolve": "NASDAQ",       # the AMEX:EMB trap, spelled out
        "tradingview_quote": "0.0945",         # the percent-not-fraction trap
        "tradingview_screen": "cannot compose",
        "tradingview_breadth": "universe",
    }
    for name, needle in must_say.items():
        described = tools[name].definition().description or ""
        assert needle in described, f"{name} lost its caveat: {described!r}"
        assert len(described) > 150, f"{name} description is a one-liner: {described!r}"


def test_a_retry_is_charged_to_the_budget_like_any_other_request() -> None:
    """A retry is a request. Checking the budget once per call, not once per
    attempt, would let a client exceed it by however many retries were in
    flight -- small here, but the guard would be stating something untrue."""
    class _Flaky:
        def __init__(self):
            self.attempts = 0

        def post(self, url, json=None):
            self.attempts += 1
            raise OSError("connection reset")

    flaky = _Flaky()
    client = ScreenerClient(transport=flaky, max_calls=1, min_interval=0)
    with pytest.raises(ScreenerError):
        client.count("america", [])
    assert flaky.attempts == 1, "the second attempt must be refused by the budget"
    assert client.calls_made == 1


def test_the_prompt_does_not_deny_what_the_tools_actually_carry() -> None:
    """It first said "no returns" while the performance bundle carries seven.

    A prompt that under-claims is not the safe direction: it makes the agent
    refuse questions it can answer, and an operator who checks the catalogue
    then has reason to distrust the rest of the prompt.
    """
    from lazytools.skills.screener import SCREENER_SYSTEM

    exposed = [f for f in BUNDLES["performance"] if f.startswith("perf_")]
    assert exposed, "the performance bundle no longer carries return figures"
    for field in exposed:
        assert field in SCREENER_SYSTEM, f"{field} is exposed but the prompt never mentions it"
    assert "no returns" not in SCREENER_SYSTEM.lower()

    # And the other direction, which the first version of this test missed: a
    # field named in the prompt but absent from every bundle is unreachable,
    # because tradingview_quote only accepts bundles.
    reachable = {f for names in BUNDLES.values() for f in names}
    for field in FIELDS:
        if field.startswith("perf_") and field in SCREENER_SYSTEM:
            assert field in reachable, f"the prompt promises {field}, no bundle carries it"


def test_a_ticker_from_another_venue_says_so_instead_of_just_vanishing() -> None:
    """'TSE:7203' asked of `america` is not "no such company"."""
    stub = _Stub(rows=[], total=0)
    out = _tools(stub).tradingview_quote("TSE:7203", bundle="core")
    assert out["not_found"] == ["TSE:7203"]
    assert "market='global'" in out["hint"]


def test_no_venue_hint_when_the_lookup_was_already_global() -> None:
    stub = _Stub(rows=[], total=0)
    out = _tools(stub, market="global").tradingview_quote("TSE:7203", bundle="core")
    assert "hint" not in out


# --------------------------------------------------------------------------- #
# Currency: price and fundamentals are not in the same one
# --------------------------------------------------------------------------- #
def _consensus_row(ticker, quote_ccy, fund_ccy, close, target):
    names = BUNDLES["consensus"]
    values = [None] * len(names)
    values[names.index("symbol")] = ticker.split(":")[1]
    values[names.index("currency")] = quote_ccy
    values[names.index("fundamental_currency")] = fund_ccy
    values[names.index("close")] = close
    values[names.index("target_avg")] = target
    return _row(ticker, values)


def test_a_mixed_currency_row_is_flagged_not_left_to_be_subtracted() -> None:
    """The sharpest edge in this endpoint, measured 2026-08-21.

    TSE:7203 closes at 3132 JPY while its mean analyst target is 23.5 USD.
    target/close - 1 reads -99.2 %; the vendor's converted figure is +19.5 %.
    Both look like percentages, only one is an answer.
    """
    stub = _Stub(rows=[_consensus_row("TSE:7203", "JPY", "USD", 3132, 23.5)], total=1)
    out = _tools(stub, market="global").tradingview_quote("TSE:7203", bundle="consensus")
    assert "local currency" in out["warning"]
    assert "TSE:7203 (JPY)" in out["warning"]
    assert "target_upside_pct" in out["warning"]


def test_the_warning_survives_the_currency_column_going_missing() -> None:
    """The old version needed both currency columns, so a renamed one silenced
    the warning in exactly the case it was written for."""
    stub = _Stub(rows=[_consensus_row("TSE:7203", None, None, 3132, 23.5)], total=1)
    out = _tools(stub, market="global").tradingview_quote("TSE:7203", bundle="consensus")
    assert "cannot even be checked" in out["warning"]


def test_a_single_currency_row_is_not_flagged() -> None:
    stub = _Stub(rows=[_consensus_row("NASDAQ:AAPL", "USD", "USD", 311.1, 333.1)], total=1)
    out = _tools(stub).tradingview_quote("NASDAQ:AAPL", bundle="consensus")
    assert "warning" not in out


def test_the_consensus_bundle_cannot_drop_the_two_currencies() -> None:
    """Without both, the mismatch is invisible and the subtraction looks safe."""
    assert "currency" in BUNDLES["consensus"]
    assert "fundamental_currency" in BUNDLES["consensus"]
    assert "target_upside_pct" in BUNDLES["consensus"]


def test_reporting_currency_fields_are_not_labelled_as_the_quote_currency() -> None:
    for field in ("target_avg", "target_high", "target_low", "eps_ttm", "revenue_ttm"):
        assert FIELDS[field].unit == "fund_ccy", f"{field} is not in the quote currency"
    assert FIELDS["close"].unit == "ccy"


# --------------------------------------------------------------------------- #
# Truncation and vacuous matches
# --------------------------------------------------------------------------- #
def test_truncation_withdraws_the_resolutions_it_cannot_vouch_for() -> None:
    """An unseen candidate is what turns a resolved ticker into an ambiguous one."""
    stub = _Stub(rows=[
        _row("NYSE:AAA", ["AAA", "Aaa Inc", "NYSE", "United States", "stock", True]),
    ], total=900)
    out = _tools(stub).tradingview_resolve("AAA")
    assert out["resolved"] == {}
    assert out["ambiguous"]["AAA"][0]["tv_ticker"] == "NYSE:AAA"
    assert "nothing here is declared" in out["warning"]


def test_missing_metadata_is_not_evidence_of_a_shared_issuer() -> None:
    """`None == None` passed the old check, so a renamed column re-opened the bug."""
    stub = _Stub(rows=[
        _row("NYSE:XYZ", ["XYZ", None, "NYSE", None, "stock", True]),
        _row("OTC:XYZ", ["XYZ", None, "OTC", None, "stock", False]),
    ], total=2)
    out = _tools(stub).tradingview_resolve("XYZ")
    assert out["resolved"] == {}
    assert len(out["ambiguous"]["XYZ"]) == 2


def test_quote_repeats_the_resolution_warning_instead_of_swallowing_it() -> None:
    stub = _Stub(rows=[
        _row("NYSE:AAA", ["AAA", "Aaa Inc", "NYSE", "United States", "stock", True]),
    ], total=900)
    out = _tools(stub).tradingview_quote("AAA", bundle="core")
    assert "warning" in out, "a caveat that stops at the inner tool protects nobody"


def test_an_unknown_unit_raises_instead_of_matching_nothing() -> None:
    tools = _tools(_Stub())
    with pytest.raises(ValueError, match="unknown unit"):
        tools.tradingview_fields(unit="percent")
    assert tools.tradingview_fields(unit="pct")["matched"] > 0


def test_both_sides_of_a_comparison_must_be_observable() -> None:
    """`close > SMA200` needs `close` too.

    Requiring only the right-hand operand left the same hole one field over: an
    instrument with no price stays in the denominator and can never reach the
    numerator, so the ratio drifts down with nothing to show for it.
    """
    stub = _Stub(total=10)
    _tools(stub).tradingview_breadth(metrics="above_sma200")
    # posts: universe, observable, hits -- the middle one carries both nempty checks
    _, observable_body = stub.posts[1]
    required = {f["left"] for f in observable_body["filter"] if f["operation"] == "nempty"}
    assert required == {"close", "SMA200"}


def test_every_metric_declares_each_field_its_clause_touches() -> None:
    for name, metric in BREADTH_METRICS.items():
        operands = {metric.clause["left"]}
        right = metric.clause.get("right")
        if isinstance(right, str):
            operands.add(right)
        missing = operands - set(metric.observable)
        assert not missing, f"{name} compares {missing} without requiring them to exist"


# --------------------------------------------------------------------------- #
# The skill wiring
# --------------------------------------------------------------------------- #
class _StubEngine:
    async def run(self, *a, **k):  # pragma: no cover - never invoked
        raise NotImplementedError


def test_the_analyst_carries_the_tools_and_a_description_a_router_can_use() -> None:
    from lazytools.skills.screener import screener_analyst

    agent = screener_analyst(engine=_StubEngine())
    assert agent.name == "screener-analyst"
    described = agent.description
    # An orchestrator picks this agent by its description alone, so the two
    # facts that decide whether delegating here is right have to be in it.
    assert "breadth" in described
    assert "no time series" in described or "cannot compute" in described


def test_the_analyst_needs_a_model_or_an_engine_and_says_which() -> None:
    from lazytools.skills.screener import screener_analyst

    with pytest.raises(ValueError, match="model="):
        screener_analyst()


def test_the_skill_form_publishes_one_typed_handle() -> None:
    from lazytools.skills.screener import screener_skill

    skill = screener_skill()
    assert skill.name == "screener"
    assert skill.reads == ()
    assert skill.writes == ("market_snapshot",)
    assert "publish(market_snapshot=" in skill.system
    tools = skill.build_tools(None)
    assert {t.name for t in tools[0].as_tools()} == TRADINGVIEW_TOOL_NAMES


def test_the_skill_is_not_in_the_default_roster() -> None:
    """Live third-party data joins a pipeline by decision, not by default."""
    from lazytools.skills.analyst import SKILLS

    assert "screener" not in {s.name for s in SKILLS}


def test_a_missing_total_count_is_refused_not_read_as_zero() -> None:
    """Zero would mean "nothing matched" AND "the page is complete".

    Both are wrong when the field is simply absent, and the second is the
    dangerous one: a truncated page that looks whole lets a ticker be declared
    uniquely resolved while its competing listings sit unread.
    """
    class _NoTotal:
        def post(self, url, json=None):
            return _Response({"data": [_row("NYSE:AAA", ["AAA"])]})

        def get(self, url):  # pragma: no cover - not reached
            raise AssertionError

    client = ScreenerClient(transport=_NoTotal(), min_interval=0)
    with pytest.raises(ScreenerError, match="usable totalCount"):
        client.scan("america", ["name"])


def test_a_total_count_smaller_than_the_page_is_refused() -> None:
    class _Inconsistent:
        def post(self, url, json=None):
            return _Response({"totalCount": 1, "data": [
                _row("NYSE:AAA", ["AAA"]), _row("NYSE:BBB", ["BBB"]),
            ]})

        def get(self, url):  # pragma: no cover - not reached
            raise AssertionError

    client = ScreenerClient(transport=_Inconsistent(), min_interval=0)
    with pytest.raises(ScreenerError, match="returned 2 rows"):
        client.scan("america", ["name"])
