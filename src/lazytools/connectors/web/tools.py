"""LazyCrawler as an LLM tool interface — a thin pass-through ToolProvider.

Per the integration decision, LazyCrawler is surfaced in lazytools **only as an
LLM tool interface**: :class:`WebTools` is a façade over LazyCrawler's own
``CrawlerTools`` tool layer. It does NOT vendor the crawler engine and does NOT
expose ``WebCrawler`` / ``AsyncWebCrawler`` — only the tools the model calls
(``list_presets``, ``web_search``, ``web_crawl``, ``get_page``, and the cache
tools when a db is configured).

``lazycrawler`` is imported lazily on first :meth:`WebTools.as_tools`, so the
connector imports cleanly without the ``web`` extra; a clear :class:`ImportError`
with an install hint is raised if it is missing and no provider was injected.
"""

from __future__ import annotations

from typing import Any


class WebTools:
    """A thin ``ToolProvider`` delegating to LazyCrawler's ``CrawlerTools``.

    Args:
        provider: A pre-built provider exposing ``as_tools()`` (typically a
            ``lazycrawler.CrawlerTools``). When ``None``, one is built lazily
            from ``lazycrawler.CrawlerTools(**crawler_kwargs)`` on first use.
        name_prefix: Optional prefix applied to each delegated tool's name
            (e.g. ``"web_"`` -> ``"web_web_search"``). Empty (default) leaves
            names untouched.
        **crawler_kwargs: Forwarded to ``CrawlerTools`` when building the
            default provider (e.g. ``db=...``, ``content="pure"``).
    """

    _is_lazy_tool_provider = True

    def __init__(self, provider: Any | None = None, *, name_prefix: str = "", **crawler_kwargs: Any) -> None:
        self._provider = provider
        self._name_prefix = name_prefix
        self._crawler_kwargs = crawler_kwargs

    # ------------------------------------------------------------------ #
    # Provider resolution (lazy: never import lazycrawler until used)
    # ------------------------------------------------------------------ #
    def _resolve(self) -> Any:
        if self._provider is None:
            try:
                from lazycrawler import CrawlerTools
            except ImportError as exc:
                raise ImportError(
                    "WebTools requires lazycrawler. "
                    'Install it with: pip install "lazycrawler @ git+https://github.com/selvaz/LazyCrawler.git" '
                    '(or: pip install "lazytoolkit[web] @ git+https://github.com/selvaz/LazyTools.git").'
                ) from exc
            self._provider = CrawlerTools(**self._crawler_kwargs)
        return self._provider

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Any]:
        tools = list(self._resolve().as_tools())
        if self._name_prefix:
            for tool in tools:
                # Tool.name is a plain settable attribute on lazybridge Tools;
                # prefix in place. If a provider yields something without a
                # settable name, leave it untouched rather than fail.
                try:
                    tool.name = f"{self._name_prefix}{tool.name}"
                except AttributeError:
                    pass
        return tools
