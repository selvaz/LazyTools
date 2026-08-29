"""The HTTP layer over Manifold Markets' public, read-only REST API.

Market reads need no API key, and this client deliberately implements no
write or order-placement surface. Manifold's list and search endpoints only
include a top-level ``probability`` for binary markets; other outcome types
do not have one there. Multiple-choice answer probabilities are exposed in
the ``answers`` array only by the single-market endpoints, so list results
have ``answers=None`` even for those markets.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

BASE_URL = "https://api.manifold.markets/v0"

USER_AGENT = "LazyTools/manifold-connector (+https://github.com/selvaz/LazyTools)"

_HTTPX_MISSING = (
    "httpx is required by the Manifold connector. Install it with the "
    "Manifold connector dependencies."
)


class ManifoldError(RuntimeError):
    """A Manifold request refused, failed, or answered something unusable."""


class ManifoldBudgetExceeded(ManifoldError):
    """The per-client call budget is spent."""


@dataclass(frozen=True)
class Market:
    """One Manifold market record normalized for connector callers."""

    id: str
    question: str
    slug: str
    url: str
    outcome_type: str
    is_resolved: bool
    created_time: str | None
    close_time: str | None
    volume: float | None
    volume_24h: float | None
    total_liquidity: float | None
    unique_bettor_count: int | None
    probability: float | None
    answers: list[dict[str, Any]] | None


def _to_market(raw: dict[str, Any]) -> Market:
    """Convert a vendor market object without failing on absent fields."""

    def _num(key: str) -> float | None:
        value = raw.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _int(key: str) -> int | None:
        value = raw.get(key)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _timestamp(key: str) -> str | None:
        value = raw.get(key)
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=UTC).isoformat()
        except (OSError, OverflowError, TypeError, ValueError):
            return None

    answers: list[dict[str, Any]] | None = None
    if "answers" in raw:
        raw_answers = raw.get("answers")
        answers = []
        if isinstance(raw_answers, list):
            for answer in raw_answers:
                if not isinstance(answer, dict):
                    continue
                probability = answer.get("probability")
                try:
                    parsed_probability = (
                        float(probability) if probability is not None else None
                    )
                except (TypeError, ValueError):
                    parsed_probability = None
                answers.append(
                    {
                        "answer": str(answer.get("text") or answer.get("answer") or ""),
                        "probability": parsed_probability,
                    }
                )

    return Market(
        id=str(raw.get("id") or ""),
        question=str(raw.get("question") or ""),
        slug=str(raw.get("slug") or ""),
        url=str(raw.get("url") or ""),
        outcome_type=str(raw.get("outcomeType") or ""),
        is_resolved=bool(raw.get("isResolved")),
        created_time=_timestamp("createdTime"),
        close_time=_timestamp("closeTime"),
        volume=_num("volume"),
        volume_24h=_num("volume24Hours"),
        total_liquidity=_num("totalLiquidity"),
        unique_bettor_count=_int("uniqueBettorCount"),
        probability=_num("probability"),
        answers=answers,
    )


class ManifoldClient:
    """A thin, budgeted client over Manifold's public REST API.

    Args:
        timeout: seconds per request.
        max_calls: how many requests this client will make before refusing.
            ``None`` removes the guard.
        min_interval: floor on the gap between two requests, with jitter.
        base_url: root of the Manifold API.
        transport: injected client duck-typed like ``httpx.Client`` (tests
            only).

    Safe to share between threads: a request is claimed under a lock.
    """

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_calls: int | None = 200,
        min_interval: float = 0.05,
        base_url: str = BASE_URL,
        transport: Any = None,
    ) -> None:
        self._timeout = timeout
        self._max_calls = max_calls
        self._min_interval = min_interval
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._calls = 0
        self._last_call = 0.0
        self._gate = threading.Lock()

    @property
    def calls_made(self) -> int:
        return self._calls

    def _client(self) -> Any:
        if self._transport is not None:
            return self._transport
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise ManifoldError(_HTTPX_MISSING) from exc
        return httpx.Client(timeout=self._timeout, headers={"User-Agent": USER_AGENT})

    def _reserve(self) -> None:
        with self._gate:
            if self._max_calls is not None and self._calls >= self._max_calls:
                raise ManifoldBudgetExceeded(
                    f"call budget of {self._max_calls} spent. Widen the question rather "
                    f"than iterating one market at a time."
                )
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.02))
            self._calls += 1
            self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Perform one GET, retried once after a transient failure."""
        client = self._client()
        url = f"{self._base_url}{path}"
        try:
            for attempt in (1, 2):
                self._reserve()
                try:
                    response = client.get(url, params=params or {})
                except Exception as exc:  # network-level
                    if attempt == 2:
                        raise ManifoldError(f"could not reach {url}: {exc}") from exc
                    time.sleep(0.3 + random.uniform(0, 0.2))
                    continue

                status = getattr(response, "status_code", 0)
                if status == 429:
                    raise ManifoldError(
                        f"{url} answered 429 (rate limited). Stop and retry later, "
                        f"do not loop."
                    )
                if status >= 500 and attempt == 1:
                    time.sleep(0.3 + random.uniform(0, 0.2))
                    continue
                if status == 404:
                    return None
                if status != 200:
                    raise ManifoldError(f"{url} answered HTTP {status}")
                try:
                    return response.json()
                except Exception as exc:
                    raise ManifoldError(f"{url} answered something that is not JSON") from exc
            raise ManifoldError(f"{url} could not be reached")  # pragma: no cover
        finally:
            if self._transport is None:
                client.close()

    def list_markets(
        self,
        *,
        limit: int = 20,
        before: str | None = None,
    ) -> list[Market]:
        """Return one page of markets ordered by most recent creation.

        Verified live: consecutive rows' ``createdTime`` is strictly
        descending while ``lastUpdatedTime`` is not sorted at all -- this
        endpoint orders by creation, not by update or activity.
        """
        params: dict[str, Any] = {"limit": limit}
        if before is not None:
            params["before"] = before
        payload = self._get("/markets", params)
        rows = payload if isinstance(payload, list) else []
        return [_to_market(row) for row in rows if isinstance(row, dict)]

    def search_markets(self, term: str, *, limit: int = 20, offset: int = 0) -> list[Market]:
        """Search markets by full-text term.

        Verified live: the endpoint accepts ``offset`` for pagination past
        the first page of matches, same as ``/markets``' ``before`` cursor
        serves ``list_markets``.
        """
        params: dict[str, Any] = {"term": term, "limit": limit}
        if offset:
            params["offset"] = offset
        payload = self._get("/search-markets", params)
        rows = payload if isinstance(payload, list) else []
        return [_to_market(row) for row in rows if isinstance(row, dict)]

    def get_market(self, market_id: str) -> Market | None:
        """Return one market by id, or ``None`` if it does not exist."""
        payload = self._get(f"/market/{market_id}")
        return _to_market(payload) if isinstance(payload, dict) else None

    def get_market_by_slug(self, slug: str) -> Market | None:
        """Return one market by slug, or ``None`` if it does not exist."""
        payload = self._get(f"/slug/{slug}")
        return _to_market(payload) if isinstance(payload, dict) else None

    def bets(self, market_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent raw bet records for one market."""
        payload = self._get("/bets", {"contractId": market_id, "limit": limit})
        if not isinstance(payload, list):
            raise ManifoldError(f"/bets returned no bet list for market {market_id!r}")
        return payload


__all__ = [
    "ManifoldClient",
    "ManifoldError",
    "ManifoldBudgetExceeded",
    "Market",
    "BASE_URL",
]
