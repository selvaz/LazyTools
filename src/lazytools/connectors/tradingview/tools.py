"""TradingView's screener as a bounded LLM tool surface.

Live and on request: these tools call the endpoint when the agent asks, and
store nothing. Two consequences worth stating to whoever reads the output.
The first is that an answer is **not reproducible** — the same question
tomorrow returns tomorrow's numbers, and there is no record of today's, so a
figure quoted in a report is unverifiable afterwards unless the caller keeps
it. The second is that everything here is a snapshot: no history, no series,
no yesterday.

What the surface deliberately does not do is pass the endpoint through. The
model chooses among named fields, named bundles, named screens and named
breadth metrics; it never composes a filter. A filter written by a model is a
universe nobody declared, and a breadth percentage over an undeclared universe
is a number that cannot be compared with anything — including its own value
from last week.

Every reply carries ``as_of``, the units of each field, and the count of calls
spent, because a number arriving without its scale and its timestamp is where
this kind of tool does its damage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lazytools.connectors.tradingview.catalog import (
    BREADTH_METRICS,
    BUNDLES,
    DATE_FIELDS,
    FIELDS,
    MARKETS,
    SCREENS,
    TIMEFRAMES,
    UNIVERSES,
    WITHHELD,
    bundle_fields,
    tv_columns,
)
from lazytools.connectors.tradingview.client import ScreenerClient, ScreenerError

#: Hard ceiling on instruments per quote call. One scan can carry hundreds, so
#: this is not a performance limit -- it is a limit on how much undigested
#: table an agent can pull into its own context in one step.
MAX_SYMBOLS = 60

#: Hard ceiling on rows a screen returns, for the same reason.
MAX_ROWS = 100

_SOURCE = "TradingView screener (scanner.tradingview.com), undocumented public endpoint"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


#: Units whose values are small enough that float64 noise shows up as digits
#: -- the endpoint really does answer 0.35000000000000003 for a 0.35 % fee.
#: Rounded at a precision far beyond anything the vendor measures, so no real
#: information is lost and the model is not handed seventeen fake digits.
_ROUNDED_UNITS = frozenset({"pct", "ratio", "score"})


def _decode(name: str, value: Any) -> Any:
    """Vendor value to something a reader can use, per field."""
    if value is None:
        return None
    if name in DATE_FIELDS:
        try:
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    spec = FIELDS.get(name)
    if spec is not None and spec.unit in _ROUNDED_UNITS and isinstance(value, float):
        return round(value, 6)
    return value


def _units(names: tuple[str, ...] | list[str]) -> dict[str, str]:
    return {n: FIELDS[n].unit for n in names if n in FIELDS}


#: The single most dangerous thing this endpoint does. On a non-US listing
#: `close` is quoted in the local currency while market cap, EPS, revenue and
#: every analyst price target come back in USD. Combining the two produces a
#: number that looks entirely reasonable and is wrong by an exchange rate:
#: measured 2026-08-21, target/close - 1 gives -99.2 % for TSE:7203 where the
#: vendor's own converted figure is +19.5 %, and +35.2 % for SIX:NESN against a
#: true +10.0 %. `market_cap / close` is wrong the same way.
_USD_UNITS = frozenset({"usd", "fund_ccy"})


def _currency_warning(rows: list[dict], names: tuple[str, ...] | list[str]) -> str | None:
    """A warning when a reply mixes locally-quoted prices with USD figures.

    Driven by the fields actually returned rather than by one column being
    present: requiring `fundamental_currency` to be non-null meant the warning
    disappeared in the very case it exists for — a column the vendor renamed.
    """
    quoted = [n for n in names if n in FIELDS and FIELDS[n].unit == "ccy"]
    usd = [n for n in names if n in FIELDS and FIELDS[n].unit in _USD_UNITS]
    if not (quoted and usd):
        return None

    mixed, unknown = [], []
    for row in rows:
        if not any(row.get(n) is not None for n in quoted):
            continue
        currency = row.get("currency")
        if currency is None:
            unknown.append(str(row.get("tv_ticker")))
        elif currency != "USD":
            mixed.append(f"{row.get('tv_ticker')} ({currency})")

    if not mixed and not unknown:
        return None
    parts = []
    if mixed:
        parts.append("prices are quoted in a local currency on " + ", ".join(sorted(mixed)[:6])
                     + (" and others" if len(mixed) > 6 else ""))
    if unknown:
        parts.append("the quote currency is unavailable for " + ", ".join(sorted(unknown)[:6])
                     + (" and others" if len(unknown) > 6 else "")
                     + ", so the mismatch cannot even be checked")
    return (
        "; ".join(parts)
        + f". These fields are in the quote currency: {', '.join(quoted)}. "
        + f"These are in USD: {', '.join(usd)}. Do NOT combine the two -- no "
        "upside, no P/E, no share count. Use target_upside_pct and pe_ttm, "
        "which the vendor converts."
    )


def _nonnull(rows: list[dict], names: tuple[str, ...] | list[str]) -> dict[str, int]:
    """How many rows carry a value, per field.

    A renamed vendor field does not raise: it turns one column entirely null,
    which reads exactly like "no fund reports this". Publishing the count per
    column is what makes the difference visible without a second request.
    """
    return {n: sum(1 for r in rows if r.get(n) is not None) for n in names if n in FIELDS}


class TradingViewTools:
    """A LazyBridge ``ToolProvider`` over the TradingView screener.

    Read-only by construction: the endpoint has no write surface, and nothing
    here persists. Build one per agent; the call budget lives on the instance.

        from lazytools.connectors.tradingview import TradingViewTools

        agent = Agent(name="screen", engine=engine, tools=[TradingViewTools()])

    Args:
        market: default market for tools that take one. ``america`` covers US
            listings including every US-listed ETF; ``global`` reaches other
            venues but returns cross-listings that must be disambiguated.
        max_calls: budget for this provider's whole life (``None`` to remove).
        timeout: seconds per request.
        client: an injected :class:`ScreenerClient`, mostly for tests.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        market: str = "america",
        max_calls: int | None = 200,
        timeout: float = 20.0,
        client: ScreenerClient | None = None,
    ) -> None:
        if market not in MARKETS:
            raise ValueError(f"unknown market {market!r}; expected one of {list(MARKETS)}")
        self._market = market
        self._client = client or ScreenerClient(max_calls=max_calls, timeout=timeout)

    # ------------------------------------------------------------------ base
    def _envelope(self, **extra: Any) -> dict:
        out = {"as_of": _now(), "source": _SOURCE, "calls_made": self._client.calls_made}
        out.update(extra)
        return out

    def _market_or(self, market: str) -> str:
        chosen = market or self._market
        if chosen not in MARKETS:
            raise ValueError(f"unknown market {chosen!r}; expected one of {list(MARKETS)}")
        return chosen

    # ------------------------------------------------------------------ tools
    def tradingview_vocabulary(self, section: str = "") -> dict:
        """What the other TradingView tools accept. Call this first.
        Every argument here is a closed vocabulary: an unknown field raises,
        but an unknown VALUE returns an empty result that reads exactly like a
        genuine "nothing matched", so a guess is the one mistake that stays
        invisible. Lists the markets, bundles, screens and breadth metrics.

        Args:
            section: one of 'fields', 'bundles', 'screens', 'breadth',
                'markets', 'enumerations'. Empty returns everything except the
                field table, which is long; ask for 'fields' to see it.

        'enumerations' reaches the endpoint for its own sector/exchange/type
        lists rather than a copy of them, and costs one call.
        """
        known = {"fields", "bundles", "screens", "breadth", "markets", "enumerations"}
        if section and section not in known:
            raise ValueError(f"unknown section {section!r}; expected one of {sorted(known)}")

        out: dict[str, Any] = {}
        if section in ("", "markets"):
            out["markets"] = list(MARKETS)
            out["timeframes"] = list(TIMEFRAMES)
        if section in ("", "bundles"):
            out["bundles"] = {k: list(v) for k, v in BUNDLES.items()}
        if section in ("", "screens"):
            out["screens"] = {
                k: {"summary": s.summary, "columns": s.columns, "markets": list(s.markets)}
                for k, s in SCREENS.items()
            }
        if section in ("", "breadth"):
            out["universes"] = {
                k: {"note": u.note, "market": u.market} for k, u in UNIVERSES.items()
            }
            out["breadth_metrics"] = {k: m.note for k, m in BREADTH_METRICS.items()}
        if section == "fields":
            out["fields"] = {
                k: {"unit": f.unit, "note": f.note, "timeframed": f.timeframed}
                for k, f in FIELDS.items()
            }
        if section in ("", "fields"):
            out["withheld"] = dict(WITHHELD)
        if section == "enumerations":
            out["enumerations"] = self._client.enumerations(
                self._market, ("sector", "industry", "exchange", "type", "typespecs")
            )
        if section == "":
            out["how_to_see_fields"] = "call tradingview_vocabulary(section='fields')"
        return self._envelope(**out)

    def tradingview_resolve(self, symbols: str, market: str = "", etf_only: bool = False) -> dict:
        """Turn plain tickers into the exchange-qualified ones the vendor uses.
        Do this before quoting anything you did not get from another
        TradingView tool: guessing the prefix looks like it works and quietly
        loses instruments. 'AMEX:EMB' returns nothing because EMB is on
        NASDAQ, and the reply is an empty row rather than an error. A ticker
        matching several listings is reported, not chosen for you.

        Args:
            symbols: comma-separated plain tickers, e.g. 'SPY,EMB,INDA'.
            market: 'america' (default) or another market; 'global' finds
                non-US listings but returns more collisions.
            etf_only: restrict to funds. Useful because a fund and an operating
                company can share a ticker across venues.

        Returns ``resolved`` (one qualified ticker each), ``ambiguous`` (every
        candidate, for you to choose between explicitly) and ``not_found``.
        A ticker that matched several listings is resolved only when exactly
        one of them is the primary listing, and the alternatives are still
        reported under ``also_listed``.
        """
        wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not wanted:
            raise ValueError("no symbols given")
        if len(wanted) > MAX_SYMBOLS:
            raise ValueError(f"at most {MAX_SYMBOLS} symbols per call; got {len(wanted)}")

        chosen = self._market_or(market)
        names = ("symbol", "description", "exchange", "country", "instrument_type", "is_primary")
        filters: list[dict] = [{"left": "name", "operation": "in_range", "right": wanted}]
        if etf_only:
            filters.append({"left": "typespecs", "operation": "has", "right": ["etf"]})

        columns = tv_columns(list(names))
        # Generous on purpose. A ticker can carry a dozen listings, and the page
        # being too small is not a slow answer -- it is a wrong one, because an
        # unseen candidate is what turns an ambiguous ticker into a confidently
        # resolved one.
        result = self._client.scan(
            chosen, columns, filter=filters, limit=min(max(len(wanted) * 12, 100), 400)
        )

        by_symbol: dict[str, list[dict]] = {}
        for raw in result.rows:
            row: dict[str, Any] = {"tv_ticker": raw["tv_ticker"]}
            for name, column in zip(names, columns, strict=True):
                row[name] = _decode(name, raw.get(column))
            by_symbol.setdefault(str(row.get("symbol") or "").upper(), []).append(row)

        resolved: dict[str, str] = {}
        also: dict[str, list[dict]] = {}
        ambiguous: dict[str, list[dict]] = {}
        for symbol in wanted:
            candidates = by_symbol.get(symbol, [])
            if not candidates:
                continue
            if len(candidates) == 1:
                resolved[symbol] = candidates[0]["tv_ticker"]
                continue
            primary = [c for c in candidates if c.get("is_primary") is True]
            # One primary is not enough to choose. `is_primary` describes a
            # single listing, not a relationship between listings: TSE:7203 is
            # Toyota and TADAWUL:7203 is Elm Company, two unrelated issuers
            # sharing a ticker. Treating the others as "also listed" would
            # present a different company as an alternative venue for this one.
            # So the same issuer has to be evidenced, not assumed -- same
            # country and same name -- and anything else stays ambiguous.
            # `None == None` is not evidence. If both columns are missing --
            # which is exactly what a renamed vendor field looks like -- a
            # vacuous match would resolve two unrelated issuers again, so the
            # primary's own values have to be present before they can agree.
            anchor = primary[0] if len(primary) == 1 else None
            same_instrument = (
                anchor is not None
                and anchor.get("country") is not None
                and anchor.get("description") is not None
                and all(
                    c.get("country") == anchor.get("country")
                    and c.get("description") == anchor.get("description")
                    for c in candidates
                )
            )
            if same_instrument and anchor is not None:
                resolved[symbol] = anchor["tv_ticker"]
                also[symbol] = [
                    {"tv_ticker": c["tv_ticker"], "description": c.get("description")}
                    for c in candidates if c is not anchor
                ]
            else:
                ambiguous[symbol] = [
                    {"tv_ticker": c["tv_ticker"], "description": c.get("description"),
                     "exchange": c.get("exchange"), "country": c.get("country"),
                     "is_primary": c.get("is_primary")}
                    for c in candidates
                ]

        # The page may not hold every match. Saying "not found" about a symbol
        # whose listing was simply past the last row read is a wrong answer
        # that looks like a right one, so truncation is reported instead.
        truncated = result.total > len(result.rows)
        if truncated:
            # Truncation does not only hide absences, it hides COMPETITORS: a
            # ticker that looks singly-listed on this page may have a second
            # listing on the next one, which would have made it ambiguous. So
            # nothing is declared resolved -- every candidate seen is offered
            # for an explicit choice instead.
            for symbol, ticker in resolved.items():
                ambiguous.setdefault(symbol, []).extend(
                    [{"tv_ticker": ticker}, *also.get(symbol, [])]
                )
            resolved, also = {}, {}

        absent = [s for s in wanted if s not in resolved and s not in ambiguous]
        out = self._envelope(
            market=chosen,
            resolved=resolved,
            also_listed=also,
            ambiguous=ambiguous,
            # Under truncation these were never looked at, which is a different
            # fact from "the endpoint does not have them" -- so they are not
            # filed under the key that says the latter.
            not_found=[] if truncated else absent,
            not_seen=absent if truncated else [],
            truncated=truncated,
            note="ambiguous tickers are not chosen for you: pass the qualified "
                 "tv_ticker you want to tradingview_quote. A ticker resolves "
                 "only when one listing is primary AND the others carry the "
                 "same issuer name and country -- a conservative rule that "
                 "will call two listings of ONE issuer ambiguous when the "
                 "vendor spells their names differently.",
        )
        if truncated:
            out["warning"] = (
                f"the endpoint matched {result.total} listings but only "
                f"{len(result.rows)} were read, so nothing here is declared resolved "
                f"and the symbols that never appeared are under 'not_seen' rather "
                f"than 'not_found'. Ask about fewer symbols at a time."
            )
        return out

    def tradingview_quote(
        self, symbols: str, bundle: str = "core", timeframe: str = "1D", market: str = ""
    ) -> dict:
        """A live snapshot of one bundle of fields for named instruments.
        Read the 'units' the reply carries before quoting any number: an
        expense ratio of 0.0945 means 0.0945 percent, and fund flows are a
        third-party estimate over a trailing window, not the day's flow.
        Nothing is stored, so this reading cannot be checked later.

        Accepts qualified tickers ('NASDAQ:AAPL') or plain ones, which are
        resolved first and cost one extra call. Mixing both is fine.

        Args:
            symbols: comma-separated, at most 60.
            bundle: 'core', 'identity', 'performance', 'technical', 'fund',
                'fundamentals' or 'consensus'. See tradingview_vocabulary.
            timeframe: '1D' (default), '1W', '1M', '1h', '4h', '15m', '5m' —
                applies only to the fields marked timeframed; the rest ignore
                it.
            market: defaults to the provider's own.

        The reply carries ``units`` for every field and ``non_null`` counts per
        field: a column that is null for every instrument means the vendor
        renamed or dropped it, which is not the same as the instruments having
        no value.
        """
        requested = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not requested:
            raise ValueError("no symbols given")
        if len(requested) > MAX_SYMBOLS:
            raise ValueError(f"at most {MAX_SYMBOLS} symbols per call; got {len(requested)}")

        chosen = self._market_or(market)
        names = bundle_fields(bundle)
        columns = tv_columns(list(names), timeframe)

        plain = [s for s in requested if ":" not in s]
        tickers = [s for s in requested if ":" in s]
        unresolved: list[str] = []
        not_seen: list[str] = []
        ambiguous: dict[str, list[dict]] = {}
        resolution_warning: str | None = None
        if plain:
            resolution = self.tradingview_resolve(",".join(plain), market=chosen)
            tickers += list(resolution["resolved"].values())
            unresolved = list(resolution["not_found"])
            not_seen = list(resolution["not_seen"])
            ambiguous = dict(resolution["ambiguous"])
            # A caveat that stops at the tool that produced it protects nobody:
            # the caller of THIS tool is the one about to quote the numbers.
            resolution_warning = resolution.get("warning")
        if not tickers:
            empty = self._envelope(
                bundle=bundle, timeframe=timeframe, market=chosen, rows=[],
                units=_units(names), not_found=unresolved, not_seen=not_seen,
                ambiguous=ambiguous,
                note="nothing resolved; call tradingview_resolve to see why.",
            )
            if resolution_warning:
                empty["warning"] = resolution_warning
            return empty

        result = self._client.scan(chosen, columns, tickers=tickers, limit=len(tickers))
        rows = []
        for raw in result.rows:
            row = {"tv_ticker": raw["tv_ticker"]}
            for name, column in zip(names, columns, strict=True):
                row[name] = _decode(name, raw.get(column))
            rows.append(row)

        returned = {r["tv_ticker"] for r in rows}
        missing = unresolved + [t for t in tickers if t not in returned]
        out = self._envelope(
            bundle=bundle, timeframe=timeframe, market=chosen,
            rows=rows, units=_units(names), non_null=_nonnull(rows, names),
            not_found=missing, not_seen=not_seen, ambiguous=ambiguous,
        )
        warnings = [w for w in (resolution_warning, _currency_warning(rows, names)) if w]
        if warnings:
            out["warning"] = " ".join(warnings)
        # A ticker is only ever looked for on ONE endpoint. 'TSE:7203' asked of
        # `america` comes back as not_found, which reads as "no such
        # instrument" when it means "not on this market" -- the same shape of
        # wrong-but-plausible answer the rest of this module exists to avoid.
        if any(":" in t for t in missing) and chosen != "global":
            out["hint"] = (
                f"a qualified ticker missing from a {chosen!r} lookup may simply "
                f"trade elsewhere; the same call with market='global' reaches "
                f"other venues."
            )
        return out

    def tradingview_screen(self, screen: str, market: str = "", limit: int = 25) -> dict:
        """A ranked list from a named, pre-defined screen.
        The filter is fixed in the definition and comes back with the result,
        so the same screen means the same thing next week. You cannot compose
        your own: an undeclared universe produces numbers that look comparable
        and are not. Call tradingview_vocabulary(section='screens') for the
        names.

        Args:
            screen: a name from tradingview_vocabulary(section='screens').
            market: must be one the screen supports.
            limit: rows, at most 100.
        """
        try:
            spec = SCREENS[screen]
        except KeyError:
            raise ValueError(
                f"unknown screen {screen!r}; call tradingview_vocabulary(section='screens')"
            ) from None
        chosen = self._market_or(market)
        if chosen not in spec.markets:
            raise ValueError(
                f"screen {screen!r} is defined for {list(spec.markets)}, not {chosen!r}"
            )
        rows_wanted = max(1, min(int(limit), MAX_ROWS))
        names = bundle_fields(spec.columns)
        columns = tv_columns(list(names))

        result = self._client.scan(
            chosen, columns, filter=list(spec.filter),
            sort_by=FIELDS[spec.sort_by].tv, ascending=spec.ascending, limit=rows_wanted,
        )
        rows = []
        for raw in result.rows:
            row = {"tv_ticker": raw["tv_ticker"]}
            for name, column in zip(names, columns, strict=True):
                row[name] = _decode(name, raw.get(column))
            rows.append(row)

        out = self._envelope(
            screen=screen, summary=spec.summary, market=chosen,
            sorted_by=spec.sort_by, ascending=spec.ascending,
            matched_total=result.total, returned=len(rows),
            rows=rows, units=_units(names), non_null=_nonnull(rows, names),
            filter_applied=list(spec.filter),
        )
        mixed = _currency_warning(rows, names)
        if mixed:
            out["warning"] = mixed
        return out

    def tradingview_breadth(
        self, universe: str = "us_cap1b", metrics: str = "", sector: str = ""
    ) -> dict:
        """How much of a universe is doing something — counts, not rows.
        Market internals: the share of a universe above its 200-day average,
        at new highs, oversold. Always report the universe with the number:
        changing it moves a breadth figure more than the market does, so a
        ratio quoted without its universe means nothing. Each ratio is over
        `n_observable`, not the whole universe: an instrument with no 200-day
        average has not failed to be above it, it cannot be asked. Costs two
        calls per metric plus one for the universe, so a sector-by-sector sweep
        is that many again per sector.

        Args:
            universe: a name from tradingview_vocabulary(section='breadth').
            metrics: comma-separated metric names; empty runs a standard set
                of four.
            sector: optional TradingView sector to narrow the universe. It
                must be spelled the vendor's way — 'Electronic Technology',
                not 'Technology'; get the list from
                tradingview_vocabulary(section='enumerations').

        The denominator and the exact filter come back with the ratio, because
        the choice of universe moves a breadth number more than the market
        does: on one verified day, 'above the 200-day average' was 58.3 % of
        US stocks over 1bn and 43.2 % of every US stock including OTC
        microcaps.
        """
        try:
            spec = UNIVERSES[universe]
        except KeyError:
            raise ValueError(
                f"unknown universe {universe!r}; call tradingview_vocabulary(section='breadth')"
            ) from None

        wanted = [m.strip() for m in metrics.split(",") if m.strip()] or [
            "above_sma50", "above_sma200", "new_high_52w", "new_low_52w"
        ]
        unknown = [m for m in wanted if m not in BREADTH_METRICS]
        if unknown:
            raise ValueError(
                f"unknown breadth metric(s) {unknown}; call "
                f"tradingview_vocabulary(section='breadth')"
            )
        if len(wanted) > len(BREADTH_METRICS):
            raise ValueError("too many metrics")

        # The universe names its own market. Counting `us_cap1b` on whatever
        # market the provider happens to be configured for would answer a
        # global question under a name that says US.
        market = spec.market
        filters = list(spec.filter)
        if sector:
            allowed = self._client.enumerations(market, ("sector",)).get("sector", [])
            if not allowed:
                # Fail closed. Accepting an unchecked sector risks a zero that
                # reads as "no stock qualifies" when it really means "no stock
                # is in a sector spelled like that".
                raise ScreenerError(
                    "the sector list could not be read from the endpoint, so "
                    f"{sector!r} cannot be checked. Retry, or drop the sector filter."
                )
            if sector not in allowed:
                raise ValueError(
                    f"unknown sector {sector!r}. TradingView's own spelling is required; "
                    f"see tradingview_vocabulary(section='enumerations')"
                )
            filters.append({"left": "sector", "operation": "equal", "right": sector})

        universe_size = self._client.count(market, filters)
        out: dict[str, Any] = {}
        for metric in wanted:
            spec_m = BREADTH_METRICS[metric]
            observable_filter = [
                *filters,
                *({"left": field, "operation": "nempty"} for field in spec_m.observable),
            ]
            observable = self._client.count(market, observable_filter)
            hits = self._client.count(market, [*observable_filter, spec_m.clause])
            out[metric] = {
                "note": spec_m.note,
                "n_hits": hits,
                "n_observable": observable,
                "n_universe": universe_size,
                "ratio_pct": round(100.0 * hits / observable, 2) if observable else None,
                "unmeasurable": universe_size - observable,
                "filter": spec_m.clause,
                "observable_fields": list(spec_m.observable),
            }
            if not observable:
                out[metric]["warning"] = (
                    f"no instrument in this universe carries all of "
                    f"{list(spec_m.observable)}, so there is no ratio to report. Either the "
                    f"universe is empty or the vendor renamed a field -- it is NOT "
                    f"'0 % of the market'."
                )

        return self._envelope(
            market=market, universe=universe, universe_note=spec.note,
            sector=sector or None, universe_filter=filters,
            n_universe=universe_size, metrics=out,
            note="each ratio is a percentage of n_observable, not of n_universe: "
                 "an instrument with no 200-day average has not failed to be "
                 "above it, it cannot be asked. 'unmeasurable' is the difference. "
                 "A different universe is a different question, not a refinement "
                 "of the same one.",
        )

    def tradingview_fields(self, search: str = "", unit: str = "") -> dict:
        """Look up which fields exist, what they mean and in what unit.
        Discovery for the bundles: when a bundle does not carry what you need,
        this says whether the field exists at all. Reading it costs nothing —
        it answers from the local catalogue, not the endpoint.

        Args:
            search: substring matched against the field name and its note.
            unit: restrict to one unit. Call with no arguments to see the
                units in use; an unknown one raises rather than quietly
                matching nothing.
        """
        known_units = {f.unit for f in FIELDS.values()}
        if unit and unit not in known_units:
            raise ValueError(
                f"unknown unit {unit!r}; the units in use are {sorted(known_units)}"
            )
        needle = search.strip().lower()
        found = {
            name: {"unit": f.unit, "note": f.note, "timeframed": f.timeframed}
            for name, f in FIELDS.items()
            if (not needle or needle in name.lower() or needle in f.note.lower())
            and (not unit or f.unit == unit)
        }
        return self._envelope(
            matched=len(found), fields=found, withheld=dict(WITHHELD),
            units_in_use=sorted(known_units),
            note="a bundle is the only way to request fields; ask for the bundle "
                 "that contains what you need.",
        )

    # ---------------------------------------------------------------- wiring
    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.tradingview_vocabulary, name="tradingview_vocabulary"),
            Tool.wrap(self.tradingview_fields, name="tradingview_fields"),
            Tool.wrap(self.tradingview_resolve, name="tradingview_resolve"),
            Tool.wrap(self.tradingview_quote, name="tradingview_quote"),
            Tool.wrap(self.tradingview_screen, name="tradingview_screen"),
            Tool.wrap(self.tradingview_breadth, name="tradingview_breadth"),
        ]


__all__ = ["TradingViewTools", "ScreenerError", "MAX_SYMBOLS", "MAX_ROWS"]
