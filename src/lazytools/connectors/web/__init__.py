"""Web connector: LazyCrawler surfaced as an LLM tool interface only.

:class:`WebTools` is a thin pass-through ``ToolProvider`` over LazyCrawler's
``CrawlerTools`` — it exposes the crawler's tool layer (``web_search``,
``web_crawl``, ``get_page``, ...) and nothing else. The crawler engine is not
vendored or re-exported. Needs the ``web`` extra (``lazycrawler``), imported
lazily on first use.
"""

from __future__ import annotations

from lazytools.connectors.web.tools import WebTools

__all__ = ["WebTools"]
