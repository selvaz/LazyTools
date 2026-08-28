"""Polymarket's public market data as a bounded LLM tool surface.

Live and on request: these tools call Gamma or CLOB when the agent asks, and
store nothing. Two things worth stating to whoever reads the output. First,
prices are not reproducible -- the same question tomorrow returns tomorrow's
odds. Second, ``polymarket_list_markets``/``polymarket_get_market`` read
Gamma, whose ``outcome_prices`` are eventually consistent and lag the live
book; a caller that needs a *current* price should call
``polymarket_price``/``polymarket_midpoint`` against CLOB instead, keyed by
the token id Gamma hands back.

Everything here is read-only by construction: placing or cancelling an order
needs a wallet signature this connector does not carry, so there is no write
tool to gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lazytools.connectors.polymarket.client import Market, PolymarketClient, PolymarketError

#: Hard ceiling on rows one listing call returns -- not a vendor limit (Gamma
#: accepts up to 500), a limit on how much undigested table an agent pulls
#: into its own context in one step.
MAX_ROWS = 100

_SOURCE = "Polymarket Gamma (gamma-api.polymarket.com) + CLOB (clob.polymarket.com), public read endpoints"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _market_dict(m: Market) -> dict[str, Any]:
    return {
        "slug": m.slug,
        "question": m.question,
        "active": m.active,
        "closed": m.closed,
        "outcomes": m.outcomes,
        # Gamma prices are strings ("0.62"); left as strings rather than
        # cast to float so a caller sees exactly what the vendor published,
        # with the staleness warning attached below rather than silently
        # implied by the type.
        "outcome_prices": m.outcome_prices,
        "clob_token_ids": m.clob_token_ids,
        "volume": m.volume,
        "volume_24hr": m.volume_24hr,
        "liquidity": m.liquidity,
        "end_date": m.end_date,
    }


class PolymarketTools:
    """A LazyBridge ``ToolProvider`` over Polymarket's public read endpoints.

    Read-only by construction: no wallet, no order placement.

        from lazytools.connectors.polymarket import PolymarketTools

        agent = Agent(name="markets", engine=engine, tools=[PolymarketTools()])

    Args:
        max_calls: budget for this provider's whole life (``None`` to remove).
        timeout: seconds per request.
        client: an injected :class:`PolymarketClient`, mostly for tests.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        max_calls: int | None = 200,
        timeout: float = 15.0,
        client: PolymarketClient | None = None,
    ) -> None:
        self._client = client or PolymarketClient(max_calls=max_calls, timeout=timeout)

    def _envelope(self, **extra: Any) -> dict:
        out = {"as_of": _now(), "source": _SOURCE, "calls_made": self._client.calls_made}
        out.update(extra)
        return out

    # ------------------------------------------------------------------ tools
    def polymarket_list_markets(
        self,
        closed: bool = False,
        tag_id: int = 0,
        order: str = "volume24hr",
        ascending: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """A ranked page of Polymarket prediction markets from the Gamma catalog.
        There is no free-text search on this endpoint: passing a keyword is
        silently ignored and the default ordering comes back instead. There
        is also no name-based category filter -- ``tag_slug`` looks plausible
        but is silently ignored too (verified live 2026-08-28); only the
        numeric ``tag_id`` actually narrows results. Look up one market by
        its exact slug with ``polymarket_get_market`` instead of guessing.
        Beyond the first page, call again with a larger ``offset``.

        Args:
            closed: False (default) returns open/unresolved markets only.
            tag_id: restrict to one numeric category tag; 0 (default) returns every category.
            order: sort key, e.g. 'volume24hr' (default), 'volume', 'endDate'.
            ascending: False (default) puts the highest value first, best for volume orderings.
            limit: rows per page, at most 100.
            offset: rows to skip; page 2 of a 20-row listing is offset=20.

        The ``outcome_prices`` returned here are Gamma's last-published
        prices, not a live quote -- they update less often than the order
        book. For a current price, take a market's ``clob_token_ids`` and
        call ``polymarket_price`` or ``polymarket_midpoint``.
        """
        rows_wanted = max(1, min(int(limit), MAX_ROWS))
        markets = self._client.markets(
            closed=closed,
            active=True if not closed else None,
            tag_id=tag_id or None,
            order=order,
            ascending=ascending,
            limit=rows_wanted,
            offset=max(0, int(offset)),
        )
        return self._envelope(
            closed=closed,
            tag_id=tag_id or None,
            order=order,
            ascending=ascending,
            offset=offset,
            returned=len(markets),
            markets=[_market_dict(m) for m in markets],
            note="outcome_prices are Gamma's last-published prices, not a live "
                 "quote; call polymarket_price/polymarket_midpoint on a "
                 "clob_token_ids entry for the current book.",
        )

    def polymarket_get_market(self, slug: str) -> dict:
        """One market's full Gamma record by its exact, stable slug.

        Args:
            slug: from ``polymarket_list_markets`` or a polymarket.com URL, e.g. 'will-trump-be-president'.

        Returns ``found=False`` rather than an error when the slug does not
        exist -- a typo'd slug should read as "no such market", not crash the
        caller's turn.
        """
        if not slug or not slug.strip():
            raise ValueError("slug is required")
        market = self._client.market(slug.strip())
        if market is None:
            return self._envelope(slug=slug, found=False)
        return self._envelope(slug=slug, found=True, market=_market_dict(market))

    def polymarket_order_book(self, token_id: str) -> dict:
        """The live CLOB order book (bids and asks) for one outcome token.
        Needs a token id, not a market slug -- get one from
        ``clob_token_ids`` on ``polymarket_list_markets``/
        ``polymarket_get_market``: each outcome (Yes, No, or one candidate in
        a multi-outcome event) has its own token id and its own book.

        Args:
            token_id: the ERC-1155 token id string for one outcome.
        """
        if not token_id or not token_id.strip():
            raise ValueError("token_id is required")
        book = self._client.book(token_id.strip())
        return self._envelope(token_id=token_id, book=book)

    def polymarket_price(self, token_id: str, side: str = "buy") -> dict:
        """The current best price on one side of the book for one outcome token.
        Counterintuitive on purpose, matching the vendor's own endpoint
        exactly (verified live 2026-08-28): ``side`` names the book side you
        are reading, not the trade you would place. 'buy' returns the best
        BID (the highest price a buyer is currently offering); 'sell'
        returns the best ASK (the lowest price a seller is currently
        asking) -- the opposite of "the price you would pay to buy".

        Args:
            token_id: the ERC-1155 token id string for one outcome, from ``clob_token_ids``.
            side: 'buy' (default) for the best bid, 'sell' for the best ask -- see above.
        """
        if not token_id or not token_id.strip():
            raise ValueError("token_id is required")
        result = self._client.price(token_id.strip(), side)
        return self._envelope(
            token_id=token_id,
            side=side,
            price=result,
            note="'buy' returns the best bid, 'sell' returns the best ask -- "
                 "not the price you would pay to execute that trade.",
        )

    def polymarket_midpoint(self, token_id: str) -> dict:
        """The order book midpoint for one outcome token -- a single fair-ish
        price estimate, halfway between best bid and best ask, cheaper to ask
        for than the full book when you only need one number.

        Args:
            token_id: the ERC-1155 token id string for one outcome, from ``clob_token_ids``.
        """
        if not token_id or not token_id.strip():
            raise ValueError("token_id is required")
        result = self._client.midpoint(token_id.strip())
        return self._envelope(token_id=token_id, midpoint=result)

    # ---------------------------------------------------------------- wiring
    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.polymarket_list_markets, name="polymarket_list_markets"),
            Tool.wrap(self.polymarket_get_market, name="polymarket_get_market"),
            Tool.wrap(self.polymarket_order_book, name="polymarket_order_book"),
            Tool.wrap(self.polymarket_price, name="polymarket_price"),
            Tool.wrap(self.polymarket_midpoint, name="polymarket_midpoint"),
        ]


__all__ = ["PolymarketTools", "PolymarketError", "MAX_ROWS"]
