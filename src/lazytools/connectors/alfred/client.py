"""The HTTP layer over ALFRED, FRED's real-time (vintage) view.

ALFRED and FRED are the same service and the same endpoints; what separates
them is the ``realtime_start``/``realtime_end`` pair. Left alone, FRED answers
with today's fully revised numbers. Pinned to a past date, it answers with
what it was publishing *on that date* — which is the only version of a series
a walk-forward backtest is entitled to see.

Two endpoints are enough:

* ``/fred/series/observations`` — the values themselves, pinned to a vintage.
* ``/fred/series/vintagedates`` — which vintages exist at all, so a caller can
  ask "what did we know the day before the decision" without guessing a date
  that never existed.

Needs a FRED API key. It reads ``FRED_API_KEY``, the same variable
market-data-hub already resolves; this deliberately does not invent a second
name for the same credential.
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

BASE_URL = "https://api.stlouisfed.org/fred"

USER_AGENT = "LazyTools/alfred-connector (+https://github.com/selvaz/LazyTools)"

_HTTPX_MISSING = (
    "httpx is required by the ALFRED connector. Install it with the "
    "extra: pip install \"lazytoolkit[alfred] @ "
    "git+https://github.com/selvaz/LazyTools.git\""
)

_NO_KEY = (
    "ALFRED needs a FRED API key. Set FRED_API_KEY (the same variable "
    "market-data-hub uses) or pass api_key= when constructing the client. "
    "Keys are free from https://fredaccount.stlouisfed.org/apikeys"
)

#: The vendor's own sentinel for "no value published for this period".
_MISSING = "."


class ALFREDError(RuntimeError):
    """An ALFRED request refused, failed, or answered something unusable."""


class ALFREDBudgetExceeded(ALFREDError):
    """The per-client call budget is spent."""


@dataclass(frozen=True)
class Observation:
    """One observation of a series, as published at one vintage."""

    date: str
    value: float | None
    as_of: str


def _number(raw: Any) -> float | None:
    """A vendor value as a float, or None for its missing-data sentinel."""
    if raw is None or raw == _MISSING:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class ALFREDClient:
    """A thin, budgeted client over FRED's real-time (ALFRED) endpoints.

    Args:
        api_key: overrides ``FRED_API_KEY``. Resolved lazily, so constructing
            a client without a key never raises — only calling does.
        timeout: seconds per request.
        max_calls: how many requests this client will make before refusing.
            ``None`` removes the guard.
        min_interval: floor on the gap between two requests, with jitter.
        base_url: API root, replaceable for tests.
        transport: injected client duck-typed like ``httpx.Client`` (tests
            only).

    Safe to share between threads: each request is claimed under a lock.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        max_calls: int | None = 200,
        min_interval: float = 0.05,
        base_url: str = BASE_URL,
        transport: Any = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._max_calls = max_calls
        self._min_interval = min_interval
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._calls = 0
        self._last_call = 0.0
        self._gate = threading.Lock()

    @property
    def calls_made(self) -> int:
        """Requests this client has issued, including retries."""
        return self._calls

    def _key(self) -> str:
        key = self._api_key or os.environ.get("FRED_API_KEY")
        if not key:
            raise ALFREDError(_NO_KEY)
        return key

    def _client(self) -> Any:
        if self._transport is not None:
            return self._transport
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise ALFREDError(_HTTPX_MISSING) from exc
        return httpx.Client(timeout=self._timeout, headers={"User-Agent": USER_AGENT})

    def _reserve(self) -> None:
        with self._gate:
            if self._max_calls is not None and self._calls >= self._max_calls:
                raise ALFREDBudgetExceeded(
                    f"call budget of {self._max_calls} spent. Ask for a date range "
                    f"in one call rather than one vintage at a time."
                )
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.02))
            self._calls += 1
            self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        """One GET, retried once on a transient failure.

        The API key is added here and never logged: an error message quotes
        the URL, so the key must not be part of the URL this method reports.
        """
        client = self._client()
        url = f"{self._base}{path}"
        query = {**params, "api_key": self._key(), "file_type": "json"}
        try:
            for attempt in (1, 2):
                self._reserve()
                try:
                    response = client.get(url, params=query)
                except Exception as exc:  # network-level
                    if attempt == 2:
                        raise ALFREDError(f"could not reach {url}: {exc}") from exc
                    time.sleep(0.3 + random.uniform(0, 0.2))
                    continue

                status = getattr(response, "status_code", 0)
                if status == 429:
                    raise ALFREDError(
                        f"{url} answered 429 (rate limited). Stop and retry later, "
                        f"do not loop."
                    )
                if status == 400:
                    # FRED reports a bad series id, an impossible date, and an
                    # invalid key all as 400 with an explanatory message. Pass
                    # the message through -- it is the useful part -- but never
                    # the query, which carries the key.
                    text = _message(response)
                    if "does not exist in ALFRED" in text:
                        # Real and common: ALFRED's vintage archive starts later
                        # than the series itself, so a vintage from before the
                        # archive begins is a legitimate question with a
                        # specific answer. The vendor's own advice here ("remove
                        # realtime_start") would silently turn a point-in-time
                        # read into a revised-data read -- precisely the mistake
                        # this connector exists to prevent -- so it is replaced
                        # rather than passed through.
                        raise ALFREDError(
                            f"{params.get('series_id')!r} has no vintage at "
                            f"{params.get('realtime_start')}: ALFRED's archive "
                            f"for it starts later than the series does. Call "
                            f"alfred_vintage_dates to see which vintages exist. "
                            f"Do not drop the vintage to make this work -- that "
                            f"returns today's revised numbers instead."
                        )
                    raise ALFREDError(f"{url} refused the request: {text}")
                if status >= 500 and attempt == 1:
                    time.sleep(0.3 + random.uniform(0, 0.2))
                    continue
                if status != 200:
                    raise ALFREDError(f"{url} answered HTTP {status}")
                try:
                    return response.json()
                except Exception as exc:
                    raise ALFREDError(f"{url} answered something that is not JSON") from exc
            raise ALFREDError(f"{url} did not answer")
        finally:
            if self._transport is None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

    def observations(
        self,
        series_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        as_of: str | None = None,
    ) -> list[Observation]:
        """Observations of ``series_id`` as published on ``as_of``.

        ``as_of=None`` asks for today's view — the revised numbers, same as a
        plain FRED call. Passing a date pins both ends of the vendor's
        realtime window to it, which is what makes the answer point-in-time.
        """
        params: dict[str, Any] = {"series_id": series_id}
        if start:
            params["observation_start"] = start
        if end:
            params["observation_end"] = end
        if as_of:
            params["realtime_start"] = as_of
            params["realtime_end"] = as_of
        payload = self._get("/series/observations", params)
        rows = payload.get("observations") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        out: list[Observation] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append(
                Observation(
                    date=str(row.get("date") or ""),
                    value=_number(row.get("value")),
                    as_of=str(row.get("realtime_start") or as_of or ""),
                )
            )
        return out

    def vintage_dates(self, series_id: str, *, limit: int = 200) -> tuple[list[str], int]:
        """The dates on which ``series_id`` was revised, newest first.

        Returns the dates AND the vendor's own total count, so a caller can
        tell a complete answer from a truncated one instead of assuming the
        list is everything. Newest-first means a truncated list drops the
        OLDEST vintages, which is the harmless end for most questions.
        """
        payload = self._get(
            "/series/vintagedates",
            {
                "series_id": series_id,
                "realtime_start": "1776-07-04",
                "realtime_end": "9999-12-31",
                "sort_order": "desc",
                "limit": limit,
                "offset": 0,
            },
        )
        if not isinstance(payload, dict):
            return [], 0
        dates = payload.get("vintage_dates")
        total = payload.get("count")
        clean = [d for d in dates if isinstance(d, str)] if isinstance(dates, list) else []
        return clean, total if isinstance(total, int) else len(clean)


def _message(response: Any) -> str:
    """FRED's own error text from a 400 body, or a neutral fallback."""
    try:
        payload = response.json()
    except Exception:
        return "no explanation given"
    if isinstance(payload, dict):
        text = payload.get("error_message")
        if isinstance(text, str) and text:
            return text
    return "no explanation given"
