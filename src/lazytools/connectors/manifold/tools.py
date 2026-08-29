"""Manifold Markets' public data as a bounded LLM tool surface.

Everything here is read-only and calls Manifold's public API on demand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lazytools.connectors.manifold.client import ManifoldClient, ManifoldError, Market

#: Hard ceiling on rows returned by one list, search, or bets call so an agent
#: cannot pull an unnecessarily large raw table into its context in one step.
MAX_ROWS = 100

_SOURCE = "Manifold Markets (api.manifold.markets), public read endpoints"

_PROBABILITY_NOTE = (
    "probability is meaningful only for BINARY markets; for MULTIPLE_CHOICE "
    "markets use answers for per-outcome probabilities."
)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _market_dict(market: Market) -> dict[str, Any]:
    return {
        "id": market.id,
        "question": market.question,
        "slug": market.slug,
        "url": market.url,
        "outcome_type": market.outcome_type,
        "is_resolved": market.is_resolved,
        "created_time": market.created_time,
        "close_time": market.close_time,
        "volume": market.volume,
        "volume_24h": market.volume_24h,
        "total_liquidity": market.total_liquidity,
        "unique_bettor_count": market.unique_bettor_count,
        "probability": market.probability,
        "answers": market.answers,
    }


class ManifoldTools:
    """A LazyBridge ``ToolProvider`` over Manifold's public read endpoints.

    Read-only by construction: this provider exposes no betting operations.

        from lazytools.connectors.manifold import ManifoldTools

        agent = Agent(name="markets", engine=engine, tools=[ManifoldTools()])

    Args:
        max_calls: budget for this provider's whole life (``None`` to remove).
        timeout: seconds per request.
        client: an injected :class:`ManifoldClient`, mostly for tests.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        max_calls: int | None = 200,
        timeout: float = 15.0,
        client: ManifoldClient | None = None,
    ) -> None:
        self._client = client or ManifoldClient(max_calls=max_calls, timeout=timeout)

    def _envelope(self, **extra: Any) -> dict:
        out = {"as_of": _now(), "source": _SOURCE, "calls_made": self._client.calls_made}
        out.update(extra)
        return out

    def manifold_list_markets(self, limit: int = 20) -> dict:
        """A page of Manifold markets sorted by most-recently-updated, not by volume or liquidity as Polymarket listings can be; this endpoint has no server-side top-by-volume ordering, so for high-activity markets use ``manifold_search_markets`` with a relevant term or sort these rows client-side by ``volume`` or ``total_liquidity``.

        Args:
            limit: number of most-recently-updated markets to return, at most 100.
        """
        rows_wanted = max(1, min(int(limit), MAX_ROWS))
        markets = self._client.list_markets(limit=rows_wanted)
        return self._envelope(
            returned=len(markets),
            markets=[_market_dict(market) for market in markets],
        )

    def manifold_search_markets(self, term: str, limit: int = 20) -> dict:
        """Search Manifold markets by a required full-text term and return matching market summaries.

        Args:
            term: non-empty full-text search phrase for market questions.
            limit: maximum number of matching markets to return, at most 100.
        """
        if not term or not term.strip():
            raise ValueError("term is required")
        rows_wanted = max(1, min(int(limit), MAX_ROWS))
        markets = self._client.search_markets(term.strip(), limit=rows_wanted)
        return self._envelope(
            term=term,
            returned=len(markets),
            markets=[_market_dict(market) for market in markets],
        )

    def manifold_get_market(self, market_id: str = "", slug: str = "") -> dict:
        """Get one full Manifold market by exactly one of id or slug; unlike list/search, this populates ``answers``, and for ``MULTIPLE_CHOICE`` use ``market["answers"]`` for per-outcome probabilities because top-level ``probability`` is ``None`` for non-binary markets.

        Args:
            market_id: exact non-empty Manifold market id; provide this or slug, but not both.
            slug: exact non-empty slug from a Manifold market URL; provide this or market_id, but not both.
        """
        has_market_id = bool(market_id and market_id.strip())
        has_slug = bool(slug and slug.strip())
        if has_market_id == has_slug:
            raise ValueError("exactly one of market_id or slug is required")

        if has_market_id:
            market = self._client.get_market(market_id.strip())
            lookup = {"market_id": market_id}
        else:
            market = self._client.get_market_by_slug(slug.strip())
            lookup = {"slug": slug}

        if market is None:
            return self._envelope(**lookup, found=False)
        return self._envelope(
            **lookup,
            found=True,
            market=_market_dict(market),
            note=_PROBABILITY_NOTE,
        )

    def manifold_probability(self, market_id: str) -> dict:
        """Get current probability data for one Manifold market id; ``probability`` is meaningful only for ``BINARY`` markets, while ``MULTIPLE_CHOICE`` callers must use ``answers`` for per-outcome probabilities.

        Args:
            market_id: exact non-empty Manifold market id whose probability data should be returned.
        """
        if not market_id or not market_id.strip():
            raise ValueError("market_id is required")
        market = self._client.get_market(market_id.strip())
        if market is None:
            return self._envelope(found=False, market_id=market_id)
        return self._envelope(
            found=True,
            market_id=market_id,
            outcome_type=market.outcome_type,
            probability=market.probability,
            answers=market.answers,
            note=_PROBABILITY_NOTE,
        )

    def manifold_recent_bets(self, market_id: str, limit: int = 20) -> dict:
        """Return recent raw bet records for one Manifold market id, newest according to the vendor endpoint.

        Args:
            market_id: exact non-empty Manifold market id whose recent bets should be returned.
            limit: maximum number of raw bet records to return, at most 100.
        """
        if not market_id or not market_id.strip():
            raise ValueError("market_id is required")
        rows_wanted = max(1, min(int(limit), MAX_ROWS))
        bets = self._client.bets(market_id.strip(), limit=rows_wanted)
        return self._envelope(
            market_id=market_id,
            returned=len(bets),
            bets=bets,
        )

    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        return [
            Tool.wrap(self.manifold_list_markets, name="manifold_list_markets"),
            Tool.wrap(self.manifold_search_markets, name="manifold_search_markets"),
            Tool.wrap(self.manifold_get_market, name="manifold_get_market"),
            Tool.wrap(self.manifold_probability, name="manifold_probability"),
            Tool.wrap(self.manifold_recent_bets, name="manifold_recent_bets"),
        ]


__all__ = ["ManifoldTools", "ManifoldError", "MAX_ROWS"]
