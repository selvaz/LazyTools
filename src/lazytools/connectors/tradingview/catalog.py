"""The closed vocabularies the TradingView tools accept.

The screener endpoint exposes 3771 fields and an arbitrary filter language.
Neither is safe to hand to a model: a guessed field name returns ``null``
(indistinguishable from a real absence), a guessed filter returns a plausible
row set built on the wrong universe, and the reply carries no units. So the
tool surface is closed — this module *is* the surface, and everything an agent
can ask for is named here.

Three things are declared per field: the vendor's own name, the **unit**, and
one line of meaning. The unit is not decoration. TradingView reports an expense
ratio of 0.0945 for a fund that charges 0.0945 % — a factor of 100 that nothing
in the payload announces, and that a model reading a bare number will get
wrong in the direction that looks reasonable.

Adding a field here is the intended way to grow the surface. Adding a *filter*
is deliberately harder: screens and breadth metrics carry their filter with
them, so the definition that produced a number stays attached to it.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Markets
# --------------------------------------------------------------------------- #
#: Scanner endpoints verified to answer. Others exist (per-country equity
#: boards, mostly); they are not listed because they are not tested, and a
#: market that 404s is a worse failure than one that was never offered.
MARKETS: tuple[str, ...] = ("america", "global", "crypto", "forex", "futures", "bond")

#: Suffix appended to a technical field to move it to another timeframe.
#: ``Recommend.All`` is the daily rating; ``Recommend.All|1W`` the weekly one.
#: The daily timeframe is the bare field, hence the empty suffix.
TIMEFRAMES: dict[str, str] = {
    "5m": "|5",
    "15m": "|15",
    "1h": "|60",
    "4h": "|240",
    "1D": "",
    "1W": "|1W",
    "1M": "|1M",
}


@dataclass(frozen=True)
class Field:
    """One readable column: vendor name, unit, and what it means."""

    tv: str
    unit: str
    note: str
    #: True when the field takes a ``TIMEFRAMES`` suffix.
    timeframed: bool = False


#: Unit vocabulary, used verbatim in every tool reply so a number never travels
#: without its scale:
#:   ``pct``      percentage points (2.5 means 2.5 %)
#:   ``ratio``    dimensionless multiple (1.5 means 1.5x)
#:   ``usd``      United States dollars
#:   ``ccy``      the instrument's own QUOTE currency, see ``currency``
#:   ``fund_ccy``  the currency the VENDOR expresses fundamentals in, see
#:                ``fundamental_currency`` — a normalisation of its own, not the
#:                issuer's reporting currency: Toyota keeps its books in yen and
#:                TradingView still answers in USD. In practice USD on every
#:                venue tested, INCLUDING non-US ones.
#:                This is the trap: on a non-US listing ``close`` is local and
#:                the fundamentals are not, so dividing one by the other is
#:                wrong by an exchange rate. Verified 2026-08-21 on TSE:7203 —
#:                close 3132 JPY against an EPS of 2.16 USD gives a P/E of
#:                1450 where the true one is 8.9.
#:   ``count``    a plain count
#:   ``datetime_utc``  ISO-8601 UTC instant, from the vendor's unix seconds.
#:                Midnight means the vendor knows the day but not the hour;
#:                20:00Z on a results date means after the US close, which
#:                is information a bare date would have thrown away.
#:   ``bool``     true or false
#:   ``text``     free text or a vendor enumeration
#:   ``score``    vendor-defined scale with no external meaning
FIELDS: dict[str, Field] = {
    # -- identity ----------------------------------------------------------
    "symbol": Field("name", "text", "Ticker as the venue lists it."),
    "description": Field("description", "text", "Instrument's full name."),
    "exchange": Field("exchange", "text", "Listing venue."),
    "country": Field("country", "text", "Country of the issuer."),
    "sector": Field("sector", "text", "TradingView sector; see tradingview_vocabulary."),
    "industry": Field("industry", "text", "TradingView industry, finer than sector."),
    "currency": Field("currency", "text", "Quote currency: the scale of close, high, low, vwap "
                      "and the moving averages."),
    "fundamental_currency": Field(
        "fundamental_currency_code", "text",
        "The currency the vendor expresses fundamentals in — revenue, EPS and the analyst price "
        "targets. It is TradingView's normalisation, NOT the issuer's own reporting currency: it "
        "reads USD for Toyota, which keeps its books in yen. When it differs from `currency`, "
        "price and fundamentals are not comparable directly."),
    "instrument_type": Field("type", "text", "stock | fund | dr | structured."),
    "is_primary": Field("is_primary", "bool", "True on the instrument's main listing. It describes ONE listing, not a link between listings: two unrelated issuers can share a ticker and each be primary on its own venue."),
    # -- price and activity ------------------------------------------------
    "close": Field("close", "ccy", "Last price."),
    "change_pct": Field("change", "pct", "Change on the previous close.", timeframed=True),
    "open": Field("open", "ccy", "Session open."),
    "high": Field("high", "ccy", "Session high."),
    "low": Field("low", "ccy", "Session low."),
    "volume": Field("volume", "count", "Shares or contracts traded.", timeframed=True),
    "avg_volume_90d": Field("average_volume_90d_calc", "count", "Mean daily volume over 90 days."),
    "relative_volume_10d": Field(
        "relative_volume_10d_calc", "ratio", "Today's volume against its 10-day mean."
    ),
    "vwap": Field("VWAP", "ccy", "Volume-weighted average price.", timeframed=True),
    "market_cap": Field("market_cap_basic", "usd", "Market capitalisation."),
    # -- performance -------------------------------------------------------
    "perf_1w": Field("Perf.W", "pct", "Return over one week."),
    "perf_1m": Field("Perf.1M", "pct", "Return over one month."),
    "perf_3m": Field("Perf.3M", "pct", "Return over three months."),
    "perf_6m": Field("Perf.6M", "pct", "Return over six months."),
    "perf_ytd": Field("Perf.YTD", "pct", "Return since 31 December."),
    "perf_1y": Field("Perf.Y", "pct", "Return over one year."),
    "perf_5y": Field("Perf.5Y", "pct", "Return over five years."),
    "price_52w_high": Field("price_52_week_high", "ccy", "Highest price in 52 weeks."),
    "price_52w_low": Field("price_52_week_low", "ccy", "Lowest price in 52 weeks."),
    "high_all_time": Field("High.All", "ccy", "Highest price on record."),
    "low_all_time": Field("Low.All", "ccy", "Lowest price on record."),
    # -- technical state ---------------------------------------------------
    "rsi": Field("RSI", "score", "Relative strength index, 0-100.", timeframed=True),
    "adx": Field("ADX", "score", "Trend strength; below 20 is trendless.", timeframed=True),
    "atr": Field("ATR", "ccy", "Average true range, in price units.", timeframed=True),
    "volatility_d": Field("Volatility.D", "pct", "Daily volatility."),
    "volatility_w": Field("Volatility.W", "pct", "Weekly volatility."),
    "volatility_m": Field("Volatility.M", "pct", "Monthly volatility."),
    "sma20": Field("SMA20", "ccy", "20-period simple moving average.", timeframed=True),
    "sma50": Field("SMA50", "ccy", "50-period simple moving average.", timeframed=True),
    "sma100": Field("SMA100", "ccy", "100-period simple moving average.", timeframed=True),
    "sma200": Field("SMA200", "ccy", "200-period simple moving average.", timeframed=True),
    "ema20": Field("EMA20", "ccy", "20-period exponential moving average.", timeframed=True),
    "ema50": Field("EMA50", "ccy", "50-period exponential moving average.", timeframed=True),
    "ema200": Field("EMA200", "ccy", "200-period exponential moving average.", timeframed=True),
    "macd": Field("MACD.macd", "ccy", "MACD line.", timeframed=True),
    "macd_signal": Field("MACD.signal", "ccy", "MACD signal line.", timeframed=True),
    "stoch_k": Field("Stoch.K", "score", "Stochastic %K.", timeframed=True),
    "bb_upper": Field("BB.upper", "ccy", "Upper Bollinger band.", timeframed=True),
    "bb_lower": Field("BB.lower", "ccy", "Lower Bollinger band.", timeframed=True),
    "rating_all": Field(
        "Recommend.All", "score",
        "Vendor's aggregate technical rating, -1 (sell) to +1 (buy). A vendor "
        "opinion, not a measurement.", timeframed=True,
    ),
    "rating_ma": Field("Recommend.MA", "score", "Same rating from moving averages only.", timeframed=True),
    "rating_oscillators": Field("Recommend.Other", "score", "Same rating from oscillators only.", timeframed=True),
    # -- funds (ETFs and other listed funds) -------------------------------
    "aum": Field("aum", "usd", "Assets under management."),
    "expense_ratio": Field(
        "expense_ratio", "pct",
        "Annual fee IN PERCENTAGE POINTS: 0.0945 means 0.0945 %, not 9.45 %.",
    ),
    "nav": Field("nav", "ccy", "Net asset value per share."),
    "nav_premium": Field("nav_discount_premium", "pct", "Price against NAV; negative is a discount."),
    "holdings_count": Field("etf_holdings_count", "count", "Number of positions held."),
    "weight_top_10": Field("weight_top_10", "pct", "Share of the fund in its ten largest positions."),
    "weight_top_25": Field("weight_top_25", "pct", "Share of the fund in its 25 largest positions."),
    "index_provider": Field("index_provider", "text", "Who publishes the tracked index."),
    "flow_1m": Field(
        "fund_flows.1M", "usd",
        "Net flow over a TRAILING month. An estimate by a third party, not a "
        "filed figure; a rolling window, so consecutive days overlap and the "
        "values are not additive.",
    ),
    "flow_3m": Field("fund_flows.3M", "usd", "Net flow over a trailing quarter; same caveats as flow_1m."),
    "flow_ytd": Field("fund_flows.YTD", "usd", "Net flow since 31 December; same caveats as flow_1m."),
    "flow_1y": Field("fund_flows.1Y", "usd", "Net flow over a trailing year; same caveats as flow_1m."),
    "nav_return_ytd": Field("nav_total_return.YTD", "pct", "Total return on NAV since 31 December."),
    "nav_return_1y": Field("nav_total_return.1Y", "pct", "Total return on NAV over one year."),
    # -- fundamentals ------------------------------------------------------
    "pe_ttm": Field("price_earnings_ttm", "ratio", "Price / trailing twelve-month earnings."),
    "pb": Field("price_book_fq", "ratio", "Price / book value, last quarter."),
    "ps_ttm": Field("price_sales_current", "ratio", "Price / trailing sales."),
    "ev_ebitda": Field("enterprise_value_ebitda_ttm", "ratio", "Enterprise value / trailing EBITDA."),
    "roe": Field("return_on_equity", "pct", "Return on equity."),
    "roa": Field("return_on_assets", "pct", "Return on assets."),
    "debt_to_equity": Field("debt_to_equity", "ratio", "Total debt / equity."),
    "current_ratio": Field("current_ratio", "ratio", "Current assets / current liabilities."),
    "gross_margin": Field("gross_margin_ttm", "pct", "Trailing gross margin."),
    "operating_margin": Field("operating_margin_ttm", "pct", "Trailing operating margin."),
    "net_margin": Field("net_margin_ttm", "pct", "Trailing net margin."),
    "revenue_ttm": Field("total_revenue_ttm", "fund_ccy", "Trailing twelve-month revenue."),
    "revenue_growth_yoy": Field("total_revenue_yoy_growth_ttm", "pct", "Trailing revenue growth year on year."),
    "eps_ttm": Field("earnings_per_share_diluted_ttm", "fund_ccy", "Trailing diluted earnings per share, in the currency the VENDOR normalises fundamentals to (USD in practice, whatever the issuer keeps its books in). Do not divide `close` by this to get a P/E on a non-US listing: use `pe_ttm`, which the vendor converts."),
    "dividend_yield": Field("dividends_yield_current", "pct", "Indicated dividend yield."),
    "beta_1y": Field("beta_1_year", "ratio", "One-year beta against the local market."),
    "shares_outstanding": Field("total_shares_outstanding_current", "count", "Shares in issue."),
    "float_shares": Field("float_shares_outstanding_current", "count", "Shares available to trade."),
    "employees": Field("number_of_employees", "count", "Headcount."),
    # -- sell-side consensus -----------------------------------------------
    "target_avg": Field("price_target_average", "fund_ccy", "Mean analyst price target, in the currency the VENDOR normalises to, not the issuer's own. Do not compare it with `close` on a non-US listing: use `target_upside_pct`, which the vendor converts."),
    "target_median": Field("price_target_median", "fund_ccy", "Median analyst price target; same currency caveat as target_avg."),
    "target_high": Field("price_target_high", "fund_ccy", "Highest analyst price target; same currency caveat as target_avg."),
    "target_low": Field("price_target_low", "fund_ccy", "Lowest analyst price target; same currency caveat as target_avg."),
    "target_upside_pct": Field("price_target_1y_delta", "pct", "Mean target against the current price, converted by the vendor. The ONLY correct upside figure here: computing it by hand gave -99.2 % for Toyota where this field gives +19.5 %."),
    "rec_buy": Field("recommendation_buy", "count", "Analysts at buy."),
    "rec_overweight": Field("recommendation_over", "count", "Analysts at overweight."),
    "rec_hold": Field("recommendation_hold", "count", "Analysts at hold."),
    "rec_underweight": Field("recommendation_under", "count", "Analysts at underweight."),
    "rec_sell": Field("recommendation_sell", "count", "Analysts at sell."),
    "rec_total": Field("recommendation_total", "count", "Analysts covering; equals the five buckets summed."),
    "rec_mark": Field("recommendation_mark", "score", "Consensus on a 1 (buy) to 5 (sell) scale."),
    # -- calendar ----------------------------------------------------------
    "earnings_next_date": Field("earnings_release_next_date", "datetime_utc", "Next expected results."),
    "earnings_last_date": Field("earnings_release_date", "datetime_utc", "Most recent results."),
    "ex_dividend_next": Field("ex_dividend_date_upcoming", "datetime_utc", "Next ex-dividend date."),
}

#: Fields whose vendor value is unix seconds, returned as an ISO instant.
DATE_FIELDS: frozenset[str] = frozenset(k for k, f in FIELDS.items() if f.unit == "datetime_utc")

#: Vendor fields deliberately NOT exposed. `asset_class`, `category` and
#: `holdings_region` come back as opaque 32-character hashes even with
#: ``lang=en`` — a model shown one would treat it as a category label.
#: Classification belongs to the caller's own reference data.
WITHHELD: dict[str, str] = {
    "asset_class": "returns an opaque hash, not a label",
    "category": "returns an opaque numeric id, not a label",
    "holdings_region": "returns an opaque hash, not a label",
    "focus_group": "empty for every instrument tested",
}

# --------------------------------------------------------------------------- #
# Bundles — named groups of fields
# --------------------------------------------------------------------------- #
BUNDLES: dict[str, tuple[str, ...]] = {
    "core": ("symbol", "description", "exchange", "currency", "close", "change_pct", "volume",
             "market_cap"),
    "activity": ("symbol", "description", "currency", "close", "change_pct", "volume",
                 "avg_volume_90d", "relative_volume_10d", "market_cap"),
    "identity": ("symbol", "description", "exchange", "country", "sector", "industry",
                 "currency", "instrument_type", "is_primary"),
    "price": ("symbol", "description", "currency", "open", "high", "low", "close",
              "change_pct", "volume", "vwap", "high_all_time", "low_all_time"),
    "moving_averages": ("symbol", "currency", "close", "sma20", "sma50", "sma100", "sma200",
                        "ema20", "ema50", "ema200"),
    "oscillators": ("symbol", "close", "rsi", "stoch_k", "macd", "macd_signal", "adx",
                    "bb_upper", "bb_lower", "volatility_d", "volatility_w", "volatility_m"),
    "performance": ("symbol", "description", "currency", "market_cap", "close", "perf_1w", "perf_1m",
                    "perf_3m", "perf_6m", "perf_ytd", "perf_1y", "perf_5y", "price_52w_high",
                    "price_52w_low"),
    "technical": ("symbol", "description", "currency", "market_cap", "close", "rsi", "adx", "atr",
                  "volatility_d", "volatility_w", "sma50", "sma200", "rating_all",
                  "rating_ma", "rating_oscillators"),
    # `currency` earns its place here too: `nav` is quoted locally while `aum`
    # and every flow come back in USD, so the mismatch has to be visible.
    "fund": ("symbol", "description", "currency", "aum", "expense_ratio", "nav", "nav_premium",
             "holdings_count", "weight_top_10", "weight_top_25", "index_provider",
             "flow_1m", "flow_3m", "flow_ytd", "flow_1y", "nav_return_ytd", "nav_return_1y",
             "perf_ytd", "avg_volume_90d"),
    "fundamentals": ("symbol", "description", "currency", "fundamental_currency", "market_cap",
                     "pe_ttm", "pb", "ps_ttm", "ev_ebitda", "roe", "roa", "debt_to_equity",
                     "current_ratio", "gross_margin", "operating_margin", "net_margin",
                     "revenue_growth_yoy", "dividend_yield", "beta_1y"),
    "financials": ("symbol", "description", "currency", "fundamental_currency", "market_cap",
                   "revenue_ttm", "eps_ttm", "shares_outstanding", "float_shares", "employees"),
    "calendar": ("symbol", "description", "market_cap", "earnings_next_date",
                 "earnings_last_date", "ex_dividend_next"),
    # currency and fundamental_currency are not optional here: `close` is in
    # the first and every target is in the second, so a bundle carrying both
    # without saying so invites exactly the subtraction that must not happen.
    "consensus": ("symbol", "currency", "fundamental_currency", "close", "target_avg", "target_median",
                  "target_high", "target_low", "target_upside_pct", "rec_buy", "rec_overweight",
                  "rec_hold", "rec_underweight", "rec_sell", "rec_total", "rec_mark"),
}


@dataclass(frozen=True)
class Screen:
    """A ranked list with its filter fixed at definition time.

    The filter travels with the result. A screen whose universe is decided by
    the caller is a screen whose numbers cannot be compared with yesterday's.
    """

    summary: str
    filter: tuple[dict, ...]
    sort_by: str
    ascending: bool
    columns: str  # a BUNDLES key
    markets: tuple[str, ...] = ("america", "global")


_STOCK = ({"left": "type", "operation": "equal", "right": "stock"},)
_ETF = ({"left": "typespecs", "operation": "has", "right": ["etf"]},)
_LIQUID = ({"left": "market_cap_basic", "operation": "egreater", "right": 1_000_000_000},)

SCREENS: dict[str, Screen] = {
    "largest_market_cap": Screen(
        "Biggest companies by market capitalisation.",
        _STOCK, "market_cap", False, "core"),
    "most_active": Screen(
        "Highest traded volume today.",
        _STOCK + _LIQUID, "volume", False, "activity"),
    "unusual_volume": Screen(
        "Trading furthest above their own 10-day average volume.",
        _STOCK + _LIQUID, "relative_volume_10d", False, "activity"),
    "top_gainers": Screen(
        "Largest gains today, above a 1bn capitalisation floor.",
        _STOCK + _LIQUID, "change_pct", False, "core"),
    "top_losers": Screen(
        "Largest falls today, above a 1bn capitalisation floor.",
        _STOCK + _LIQUID, "change_pct", True, "core"),
    "new_52w_highs": Screen(
        "Trading at or above their 52-week high.",
        _STOCK + _LIQUID + ({"left": "price_52_week_high", "operation": "eless", "right": "close"},),
        "market_cap", False, "performance"),
    "new_52w_lows": Screen(
        "Trading at or below their 52-week low.",
        _STOCK + _LIQUID + ({"left": "price_52_week_low", "operation": "egreater", "right": "close"},),
        "market_cap", False, "performance"),
    "oversold_rsi": Screen(
        "Daily RSI below 30, above a 1bn capitalisation floor.",
        _STOCK + _LIQUID + ({"left": "RSI", "operation": "less", "right": 30},),
        "market_cap", False, "technical"),
    "overbought_rsi": Screen(
        "Daily RSI above 70, above a 1bn capitalisation floor.",
        _STOCK + _LIQUID + ({"left": "RSI", "operation": "greater", "right": 70},),
        "market_cap", False, "technical"),
    "highest_dividend_yield": Screen(
        "Highest indicated dividend yield, above a 1bn capitalisation floor.",
        _STOCK + _LIQUID + ({"left": "dividends_yield_current", "operation": "nempty"},),
        "dividend_yield", False, "fundamentals"),
    "most_analyst_upside": Screen(
        "Mean price target furthest above the current price. A vendor "
        "aggregate of third-party opinion, not a measurement.",
        _STOCK + _LIQUID + ({"left": "recommendation_total", "operation": "egreater", "right": 5},),
        "target_upside_pct", False, "consensus"),
    "etf_largest_aum": Screen(
        "Biggest funds by assets under management.",
        _ETF, "aum", False, "fund", markets=("america",)),
    "etf_largest_inflows_1m": Screen(
        "Funds with the largest estimated net inflow over a trailing month.",
        _ETF, "flow_1m", False, "fund", markets=("america",)),
    "etf_largest_outflows_1m": Screen(
        "Funds with the largest estimated net outflow over a trailing month.",
        _ETF, "flow_1m", True, "fund", markets=("america",)),
    "etf_cheapest": Screen(
        "Lowest expense ratio among funds holding at least 1bn.",
        (*_ETF, {"left": "aum", "operation": "egreater", "right": 1_000_000_000}),
        "expense_ratio", True, "fund", markets=("america",)),
}

# --------------------------------------------------------------------------- #
# Breadth — counts, not rows
# --------------------------------------------------------------------------- #
#: A breadth number is a fraction of a universe, and changing the universe
#: changes the answer more than any market move does: on 2026-08-20, "above the
#: 200-day average" was 58.3 % of US stocks over 1bn and 43.2 % of every US
#: stock including OTC microcaps. So the universe is named, closed, and
#: reported alongside every number.
@dataclass(frozen=True)
class Universe:
    """A denominator: which market, and which rows inside it.

    The market is part of the definition, not a caller's choice. The filters
    below say "a stock above a capitalisation floor" and nothing about being
    American — what makes ``us_cap1b`` American is that it is counted on the
    ``america`` endpoint. Letting a caller point the same name at ``global``
    would return world numbers under a name that says US, which is the exact
    failure a named universe exists to prevent.
    """

    note: str
    filter: tuple[dict, ...]
    market: str


UNIVERSES: dict[str, Universe] = {
    "us_all": Universe(
        "Every US-listed stock the scanner carries, OTC and microcaps included. "
        "Breadth over this universe is dominated by illiquid names and runs "
        "persistently weaker than the investable market: us_cap1b is the usual "
        "reference.", _STOCK, "america"),
    "us_cap1b": Universe("US stocks capitalised above 1bn.", _STOCK + _LIQUID, "america"),
    "us_cap10b": Universe(
        "US stocks capitalised above 10bn.",
        (*_STOCK, {"left": "market_cap_basic", "operation": "egreater", "right": 10_000_000_000}),
        "america"),
    "us_etf": Universe("US-listed ETFs.", _ETF, "america"),
}

@dataclass(frozen=True)
class Metric:
    """A breadth question, and the field an instrument needs to answer it.

    ``observable`` is what stops missing data from voting. A company listed
    three months ago has no 200-day average, and a filter for "price above
    SMA200" simply does not match it — so counting it in the denominator
    silently files it under "not above", which is not what it is. It is
    unmeasurable, and the honest denominator is the instruments that could
    have answered. It also makes a renamed vendor field visible: the
    observable count collapses to zero instead of the ratio quietly reading
    0 %.

    It lists EVERY field the clause touches, both sides of the comparison.
    Checking only the right-hand one leaves the same hole one operand over: a
    missing ``close`` keeps an instrument in the denominator while making the
    numerator impossible for it.
    """

    note: str
    clause: dict
    observable: tuple[str, ...]  # vendor fields that must all be non-empty


BREADTH_METRICS: dict[str, Metric] = {
    "above_sma20": Metric("Price above the 20-day average.",
                          {"left": "close", "operation": "greater", "right": "SMA20"}, ("close", "SMA20")),
    "above_sma50": Metric("Price above the 50-day average.",
                          {"left": "close", "operation": "greater", "right": "SMA50"}, ("close", "SMA50")),
    "above_sma200": Metric("Price above the 200-day average.",
                           {"left": "close", "operation": "greater", "right": "SMA200"}, ("close", "SMA200")),
    "new_high_52w": Metric("At or above the 52-week high.",
                           {"left": "price_52_week_high", "operation": "eless", "right": "close"},
                           ("close", "price_52_week_high")),
    "new_low_52w": Metric("At or below the 52-week low.",
                          {"left": "price_52_week_low", "operation": "egreater", "right": "close"},
                          ("close", "price_52_week_low")),
    "at_all_time_high": Metric("At or above the all-time high.",
                               {"left": "High.All", "operation": "eless", "right": "close"},
                               ("close", "High.All")),
    "rsi_oversold": Metric("Daily RSI below 30.",
                           {"left": "RSI", "operation": "less", "right": 30}, ("RSI",)),
    "rsi_overbought": Metric("Daily RSI above 70.",
                             {"left": "RSI", "operation": "greater", "right": 70}, ("RSI",)),
    "up_today": Metric("Positive change on the session.",
                       {"left": "change", "operation": "greater", "right": 0}, ("change",)),
    "down_today": Metric("Negative change on the session.",
                         {"left": "change", "operation": "less", "right": 0}, ("change",)),
}


# --------------------------------------------------------------------------- #
# Resolution helpers
# --------------------------------------------------------------------------- #
def tv_columns(names: list[str] | tuple[str, ...], timeframe: str = "1D") -> list[str]:
    """Our field names to the vendor's, applying a timeframe where it applies.

    Raises on an unknown name rather than passing it through: the endpoint
    answers an unknown column with ``null``, which reads exactly like a real
    missing value and would be believed.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {timeframe!r}; expected one of {sorted(TIMEFRAMES)}")
    suffix = TIMEFRAMES[timeframe]
    out: list[str] = []
    for name in names:
        field = FIELDS.get(name)
        if field is None:
            hint = WITHHELD.get(name)
            if hint:
                raise ValueError(f"field {name!r} is deliberately not exposed: {hint}")
            raise ValueError(f"unknown field {name!r}; call tradingview_vocabulary for the list")
        out.append(field.tv + suffix if (field.timeframed and suffix) else field.tv)
    return out


def bundle_fields(bundle: str) -> tuple[str, ...]:
    """The field names in a named bundle."""
    try:
        return BUNDLES[bundle]
    except KeyError:
        raise ValueError(f"unknown bundle {bundle!r}; expected one of {sorted(BUNDLES)}") from None
