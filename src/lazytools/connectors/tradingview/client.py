"""The HTTP layer for TradingView's screener endpoint.

One POST, one JSON body, no key. The endpoint is undocumented and carries no
contract: field names can change, and the only thing separating "this fund
reports no flows" from "this field was renamed last night" is that the second
case turns a whole column null at once. Nothing here can detect that on a
single call — :mod:`tools` reports per-column non-null counts so the caller
can.

Deliberately not implemented: aggressive retry. A 429 or a 403 from a service
that owes us nothing is an instruction to stop, not a suggestion to try
harder. Transient network failures get exactly one retry.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

BASE_URL = "https://scanner.tradingview.com"

#: Sent so the request is attributable rather than anonymous. Not a disguise:
#: the endpoint answers without it too.
USER_AGENT = "LazyTools/tradingview-connector (+https://github.com/selvaz/LazyTools)"

_HTTPX_MISSING = (
    "httpx is required by the TradingView connector. Install it with the "
    "extra: pip install \"lazytoolkit[tradingview] @ "
    "git+https://github.com/selvaz/LazyTools.git\""
)


class ScreenerError(RuntimeError):
    """The endpoint refused, failed, or answered something unusable."""


class ScreenerBudgetExceeded(ScreenerError):
    """The per-client call budget is spent.

    A guard against a loop — an agent iterating a list of two hundred symbols
    one call at a time — rather than against the endpoint's own limits, which
    are unpublished.
    """


@dataclass
class ScanResult:
    """What one scan returned, and how much of it there was."""

    #: Rows the endpoint matched in total, which is usually far more than the
    #: page returned. Breadth counting reads only this.
    total: int
    #: One dict per row: ``{"tv_ticker": "NASDAQ:AAPL", <field>: <value>, ...}``.
    rows: list[dict[str, Any]] = field(default_factory=list)


class ScreenerClient:
    """A thin, budgeted client over ``scanner.tradingview.com``.

    Args:
        timeout: seconds per request.
        max_calls: how many requests this client will make before refusing.
            ``None`` removes the guard; the default is generous for one
            conversation and small enough to stop a runaway loop.
        min_interval: floor on the gap between two requests, with jitter, so a
            burst of tool calls does not arrive as a burst of traffic.

    Safe to share between threads: a request is claimed under a lock, so a
    budget of N stays N even when an MCP server drives several tools at once,
    and the minimum interval really spaces the traffic instead of being
    something every thread reads at the same moment.
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_calls: int | None = 200,
        min_interval: float = 0.12,
        base_url: str = BASE_URL,
        transport: Any = None,
    ) -> None:
        self._timeout = timeout
        self._max_calls = max_calls
        self._min_interval = min_interval
        self._base = base_url.rstrip("/")
        self._transport = transport  # tests inject a stub here
        self._calls = 0
        self._last_call = 0.0
        self._gate = threading.Lock()
        self._metainfo: dict[str, dict] = {}

    # ------------------------------------------------------------------ http
    @property
    def calls_made(self) -> int:
        """Requests this client has issued. Reported with every tool result."""
        return self._calls

    def _client(self) -> Any:
        if self._transport is not None:
            return self._transport
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise ScreenerError(_HTTPX_MISSING) from exc
        return httpx.Client(timeout=self._timeout, headers={"User-Agent": USER_AGENT})

    def _reserve(self) -> None:
        """Claim one request: check the budget, space it, count it — atomically.

        Checking and counting in two steps is fine for one agent and wrong for
        the MCP server, where every tool on the provider shares this client and
        parallel calls can all pass the check before any of them increments. A
        budget of one would then issue two requests, and the interval guard
        would let exactly the burst it exists to prevent. The wait happens
        inside the lock on purpose: serialising the requests IS the spacing.
        """
        with self._gate:
            if self._max_calls is not None and self._calls >= self._max_calls:
                raise ScreenerBudgetExceeded(
                    f"call budget of {self._max_calls} spent. Widen the question rather "
                    f"than iterating: one scan can carry hundreds of instruments."
                )
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.05))
            self._calls += 1
            self._last_call = time.monotonic()

    def _request(self, verb: str, path: str, body: dict | None = None) -> dict:
        """One request, retried once on a transient failure, always a dict.

        Every path out of here is a :class:`ScreenerError`. Both callers go
        through it so ``metainfo`` cannot quietly acquire different error
        behaviour from ``scan`` — the two used to differ, and the one that was
        wrong was the one nobody read.
        """
        client = self._client()
        try:
            for attempt in (1, 2):
                # Per attempt, not per call: a retry is a request like any
                # other, and checking only once would let the budget be
                # exceeded by however many retries were in flight.
                self._reserve()
                try:
                    response = (
                        client.post(f"{self._base}{path}", json=body)
                        if verb == "POST"
                        else client.get(f"{self._base}{path}")
                    )
                except Exception as exc:  # network-level
                    if attempt == 2:
                        raise ScreenerError(f"could not reach the screener: {exc}") from exc
                    time.sleep(0.4 + random.uniform(0, 0.3))
                    continue

                status = getattr(response, "status_code", 0)
                if status in (429, 403):
                    raise ScreenerError(
                        f"the screener answered {status}. This endpoint is undocumented "
                        f"and rate limits are unpublished: stop and retry much later, "
                        f"do not loop."
                    )
                if status >= 500 and attempt == 1:
                    time.sleep(0.4 + random.uniform(0, 0.3))
                    continue
                if status != 200:
                    raise ScreenerError(f"the screener answered HTTP {status}")
                try:
                    payload = response.json()
                except Exception as exc:
                    raise ScreenerError("the screener answered something that is not JSON") from exc
                if not isinstance(payload, dict):
                    raise ScreenerError(
                        f"the screener answered a {type(payload).__name__}, not an object"
                    )
                return payload
            raise ScreenerError("the screener could not be reached")  # pragma: no cover
        finally:
            if self._transport is None:
                client.close()

    # ------------------------------------------------------------------ api
    def scan(
        self,
        market: str,
        columns: list[str],
        *,
        filter: list[dict] | None = None,
        tickers: list[str] | None = None,
        sort_by: str | None = None,
        ascending: bool = False,
        start: int = 0,
        limit: int = 100,
    ) -> ScanResult:
        """One page of a screen, plus how many rows matched in total."""
        body: dict[str, Any] = {
            "columns": columns,
            "options": {"lang": "en"},
            "range": [start, start + max(limit, 1)],
        }
        if filter:
            body["filter"] = list(filter)
        if tickers:
            body["symbols"] = {"tickers": list(tickers)}
        if sort_by:
            body["sort"] = {"sortBy": sort_by, "sortOrder": "asc" if ascending else "desc"}

        payload = self._request("POST", f"/{market}/scan", body)
        if payload.get("error"):
            raise ScreenerError(f"the screener rejected the request: {payload['error']}")

        rows: list[dict[str, Any]] = []
        for row in payload.get("data") or []:
            values = row.get("d") or []
            item: dict[str, Any] = {"tv_ticker": row.get("s")}
            # strict: the endpoint returns one value per requested column. A
            # short row would silently shift every field one place left --
            # an AUM landing in an expense ratio -- so it must raise.
            item.update(dict(zip(columns, values, strict=True)))
            rows.append(item)

        # `totalCount` is load-bearing twice over: it IS the answer for every
        # breadth count, and it is what tells a caller its page was partial.
        # Defaulting a missing one to zero would make a truncated page look
        # complete, which is how a ticker gets declared uniquely resolved
        # while its competing listings sit unread. So it is validated rather
        # than coerced.
        total = payload.get("totalCount")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ScreenerError(
                f"the screener answered without a usable totalCount ({total!r}); "
                f"the result cannot be trusted to be complete"
            )
        if total < len(rows):
            raise ScreenerError(
                f"the screener reported {total} matches but returned {len(rows)} rows"
            )
        return ScanResult(total=total, rows=rows)

    def count(self, market: str, filter: list[dict]) -> int:
        """How many rows match, without carrying any of them back.

        The whole of breadth is this call: a percentage of a universe is two
        counts, not two thousand rows.
        """
        return self.scan(market, ["name"], filter=filter, limit=1).total

    def metainfo(self, market: str) -> dict:
        """The endpoint's own field catalogue, cached per client.

        Its ``r`` lists are the authoritative enumerations for text fields
        (sectors, exchanges, instrument types) — which is why the vocabulary
        tool reads them from here instead of hard-coding a copy that drifts.
        """
        if market in self._metainfo:
            return self._metainfo[market]
        payload = self._request("GET", f"/{market}/metainfo")
        self._metainfo[market] = payload
        return payload

    def enumerations(self, market: str, names: tuple[str, ...]) -> dict[str, list[str]]:
        """The allowed values of the named text fields, from metainfo."""
        wanted = set(names)
        out: dict[str, list[str]] = {}
        for spec in self.metainfo(market).get("fields") or []:
            key = spec.get("n")
            if key in wanted and isinstance(spec.get("r"), list):
                out[key] = [v for v in spec["r"] if isinstance(v, str) and v]
        return out
