"""Backend seam for the market-data-hub connector.

:class:`DataHubBackend` is the :class:`~typing.Protocol` that
:class:`~lazytools.connectors.datahub.tools.DataHubTools` talks to — every
method maps 1:1 to a market-data-hub ``tool_*`` function and returns the JSON
string that function already produces. The default
:class:`MarketDataHubBackend` lazily imports ``market_data_hub`` and binds its
``tool_*`` functions, so this module (and the whole connector) imports cleanly
without the ``datahub`` extra and is fully testable against a fake backend.
"""

from __future__ import annotations

from typing import Any, Protocol


class DataHubBackend(Protocol):
    """The callables the datahub tools invoke (each returns a JSON string).

    Mirrors market-data-hub's ``tool_*`` discovery + extraction surface; the
    method names drop the ``tool_`` prefix. A concrete backend (the real
    :class:`MarketDataHubBackend` or a test fake) supplies these.
    """

    def list_datasets(self) -> str: ...
    def list_symbols(self, asset_class: str = "", area: str = "", sector: str = "", group: str = "") -> str: ...
    def list_sectors(self, area: str = "") -> str: ...
    def list_macro(self, frequency: str = "", category: str = "") -> str: ...
    def list_indicators(self, pillar: str = "") -> str: ...
    def list_countries(self, region: str = "", income: str = "") -> str: ...
    def describe(self, symbol_or_id: str) -> str: ...
    def search(self, query: str) -> str: ...
    def get_series(
        self,
        symbols: str,
        start: str = "",
        end: str = "",
        domain: str = "prices",
        field: str = "adj_close",
        transform: str = "level",
        frequency: str = "",
    ) -> str: ...
    def get_returns(self, symbols: str, start: str = "", end: str = "", frequency: str = "W") -> str: ...
    def get_coverage(self, symbols: str = "") -> str: ...
    def resolve_instrument(self, query: str, exchange: str = "", currency: str = "") -> str: ...
    def get_price_summary(self, query: str, start: str = "", end: str = "") -> str: ...
    def get_financials_coverage(self, query: str = "") -> str: ...
    def get_financial_facts(self, query: str, line: str = "", forms: str = "", limit: int = 25) -> str: ...
    def get_statement(self, query: str, statement: str = "", periods: int = 8) -> str: ...
    def get_job_status(self, job_id: str) -> str: ...
    def get_ingestion_health(self) -> str: ...
    def refresh_prices(self, symbols: str, start: str = "2010-01-01") -> str: ...
    def ensure_price_history(self, query: str, start: str = "", end: str = "") -> str: ...
    def ensure_financials(self, query: str) -> str: ...


class MarketDataHubBackend:
    """Default :class:`DataHubBackend` backed by the ``market_data_hub`` package.

    ``market_data_hub`` is imported lazily on construction; if it is not
    installed a clear :class:`ImportError` with an install hint is raised. Each
    method simply forwards to the matching ``tool_*`` function, which returns a
    JSON string.
    """

    def __init__(self) -> None:
        try:
            from market_data_hub import agent_tools
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "MarketDataHubBackend requires market-data-hub (a private, "
                "git-installed package): pip install "
                "'market-data-hub @ git+https://github.com/selvaz/market-data-hub.git'"
            ) from exc
        self._mdh: Any = agent_tools

    def list_datasets(self) -> str:
        return self._mdh.tool_list_datasets()

    def list_symbols(self, asset_class: str = "", area: str = "", sector: str = "", group: str = "") -> str:
        return self._mdh.tool_list_symbols(asset_class=asset_class, area=area, sector=sector, group=group)

    def list_sectors(self, area: str = "") -> str:
        return self._mdh.tool_list_sectors(area=area)

    def list_macro(self, frequency: str = "", category: str = "") -> str:
        return self._mdh.tool_list_macro(frequency=frequency, category=category)

    def list_indicators(self, pillar: str = "") -> str:
        return self._mdh.tool_list_indicators(pillar=pillar)

    def list_countries(self, region: str = "", income: str = "") -> str:
        return self._mdh.tool_list_countries(region=region, income=income)

    def describe(self, symbol_or_id: str) -> str:
        return self._mdh.tool_describe(symbol_or_id)

    def search(self, query: str) -> str:
        return self._mdh.tool_search(query)

    def get_series(
        self,
        symbols: str,
        start: str = "",
        end: str = "",
        domain: str = "prices",
        field: str = "adj_close",
        transform: str = "level",
        frequency: str = "",
    ) -> str:
        return self._mdh.tool_get_series(
            symbols, start=start, end=end, domain=domain, field=field, transform=transform, frequency=frequency
        )

    def get_returns(self, symbols: str, start: str = "", end: str = "", frequency: str = "W") -> str:
        return self._mdh.tool_get_returns(symbols, start=start, end=end, frequency=frequency)

    def get_coverage(self, symbols: str = "") -> str:
        return self._mdh.tool_get_coverage(symbols)

    def resolve_instrument(self, query: str, exchange: str = "", currency: str = "") -> str:
        return self._mdh.tool_resolve_instrument(query, exchange=exchange, currency=currency)

    def get_price_summary(self, query: str, start: str = "", end: str = "") -> str:
        return self._mdh.tool_get_price_summary(query, start=start, end=end)

    def get_financials_coverage(self, query: str = "") -> str:
        return self._mdh.tool_get_financials_coverage(query)

    def get_financial_facts(self, query: str, line: str = "", forms: str = "", limit: int = 25) -> str:
        return self._mdh.tool_get_financial_facts(query, line=line, forms=forms, limit=limit)

    def get_statement(self, query: str, statement: str = "", periods: int = 8) -> str:
        return self._mdh.tool_get_statement(query, statement=statement, periods=periods)

    def get_job_status(self, job_id: str) -> str:
        return self._mdh.tool_get_job_status(job_id)

    def get_ingestion_health(self) -> str:
        return self._mdh.tool_get_ingestion_health()

    # Write capabilities: the hub gates every write behind allow_write; this
    # backend passes it explicitly because DataHubTools only surfaces these
    # methods when the provider itself was built with the write flag on.
    def refresh_prices(self, symbols: str, start: str = "2010-01-01") -> str:
        return self._mdh.tool_refresh_prices(symbols, start=start, allow_write=True)

    def ensure_price_history(self, query: str, start: str = "", end: str = "") -> str:
        return self._mdh.tool_ensure_price_history(query, start=start, end=end, allow_write=True)

    def ensure_financials(self, query: str) -> str:
        return self._mdh.tool_ensure_financials(query, allow_write=True)
