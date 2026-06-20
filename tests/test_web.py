"""WebTools is a thin pass-through over LazyCrawler's CrawlerTools (no lazycrawler)."""

from __future__ import annotations

import builtins

import pytest
from lazybridge import Tool

from lazytools.connectors.web import WebTools


class FakeCrawlerProvider:
    """Stand-in for ``lazycrawler.CrawlerTools`` — yields a couple of real Tools."""

    _is_lazy_tool_provider = True

    def __init__(self) -> None:
        self.as_tools_calls = 0

    def _web_search(self, query: str) -> str:
        return f"searched:{query}"

    def _get_page(self, url: str) -> str:
        return f"page:{url}"

    def as_tools(self) -> list[Tool]:
        self.as_tools_calls += 1
        return [
            Tool.wrap(self._web_search, name="web_search", description="search the web"),
            Tool.wrap(self._get_page, name="get_page", description="fetch a page"),
        ]


def test_provider_is_tool_provider() -> None:
    assert WebTools(provider=FakeCrawlerProvider())._is_lazy_tool_provider is True


def test_delegates_to_injected_provider() -> None:
    fake = FakeCrawlerProvider()
    tools = WebTools(provider=fake).as_tools()
    assert {t.name for t in tools} == {"web_search", "get_page"}
    assert fake.as_tools_calls == 1
    # The delegated tools really run.
    by_name = {t.name: t for t in tools}
    assert by_name["web_search"].run_sync(query="ai act") == "searched:ai act"


def test_name_prefix_applied_to_delegated_tools() -> None:
    tools = WebTools(provider=FakeCrawlerProvider(), name_prefix="web_").as_tools()
    assert {t.name for t in tools} == {"web_web_search", "web_get_page"}


def test_empty_prefix_leaves_names_unchanged() -> None:
    tools = WebTools(provider=FakeCrawlerProvider(), name_prefix="").as_tools()
    assert {t.name for t in tools} == {"web_search", "get_page"}


def test_missing_lazycrawler_raises_helpful_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "lazycrawler" or name.startswith("lazycrawler."):
            raise ImportError("No module named 'lazycrawler'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    web = WebTools()  # no provider -> must lazily import lazycrawler on use
    with pytest.raises(ImportError, match="pip install lazycrawler"):
        web.as_tools()
