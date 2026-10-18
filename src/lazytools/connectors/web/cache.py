"""Interactive web-cache isolation for the LazyCrawler tools (opt-in).

Production agents (LazyCommittee's scheduled searchers, the news jobs) write
their pages to the production crawl store (``LAZYCRAWLER_NEWS_DB``). Interactive
use -- a Claude Code session, one-off research -- must not: its pages would sit
in the same ``pages`` / ``sessions`` tables with nothing marking them as
interactive, and its writes contend with the scheduled crawl's lock.

This module is the opt-in switch. It only does anything when
``LAZYTOOLS_WEB_CACHE_DB`` is set (see :func:`web_cache_requested`); unset, the
caller keeps today's behaviour and never imports anything from here but the
predicate. When it is set:

* the fetch tools (``web_search``, ``web_crawl``, ``web_crawl_many``) write to a
  separate, disposable SQLite file (default ``~/.lazytools/web_cache.sqlite``),
  or to an in-process memory DB when the value is ``none`` (nothing on disk);
* the production store, if ``LAZYCRAWLER_NEWS_DB`` names an existing file, is
  opened strictly read-only (SQLite ``mode=ro``: no writes, no WAL pragma, no
  schema DDL, no migrations) and only ever *read* by the cache tools;
* a cache path that is the production file (or one of its ``-wal``/``-shm``/
  ``-journal`` side files) or lies under ``C:\\ProgramData\\InvestmentCommittee``
  is refused with :class:`WebCachePathError` before anything is opened.

Merge semantics of the read tools (``LayeredReadDB``): the interactive cache is
consulted first, then production; there is no write-through and no copying.

==================  ==========================================================
``get_page``        interactive row if present, else the production row
``get_session_pages``/``get_crawl_graph``/``get_artifacts``
                    interactive if it has any rows for the key, else production
``search_cached``   interactive hits first, then production hits, de-duplicated
                    by page (url hash), truncated to ``limit``
``get_cache_stats`` ``{"interactive": {...}, "production": {...} | null}``
==================  ==========================================================
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

_log = logging.getLogger("lazytools.web_cache")

#: Env var that switches interactive isolation on (and names the cache file).
ENV_WEB_CACHE_DB = "LAZYTOOLS_WEB_CACHE_DB"
#: The production crawl store (LazyCrawler's own variable).
ENV_NEWS_DB = "LAZYCRAWLER_NEWS_DB"
#: Values of ``LAZYTOOLS_WEB_CACHE_DB`` that mean "keep nothing on disk".
NO_CACHE_VALUES = frozenset({"none", ":memory:"})
#: Values that mean "use the default path" (an empty value does too).
DEFAULT_VALUES = frozenset({"", "default"})
#: Directories the interactive cache may never live in.
PROTECTED_DIRS: tuple[str, ...] = (r"C:\ProgramData\InvestmentCommittee",)

_SIDE_SUFFIXES = ("", "-wal", "-shm", "-journal")


class WebCachePathError(ValueError):
    """The interactive web-cache path would alias the production store."""


# --------------------------------------------------------------------------- #
# Resolution + guard
# --------------------------------------------------------------------------- #
def web_cache_requested(explicit: str | None = None) -> bool:
    """True when interactive isolation is switched on (variable or explicit value set)."""
    return explicit is not None or os.environ.get(ENV_WEB_CACHE_DB) is not None


def default_web_cache_path() -> str:
    """``~/.lazytools/web_cache.sqlite`` (``LAZYTOOLS_DATA_DIR`` replaces ``~/.lazytools``)."""
    home = os.environ.get("LAZYTOOLS_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".lazytools")
    return os.path.join(home, "web_cache.sqlite")


def resolve_web_cache_path(explicit: str | None = None) -> str | None:
    """The interactive cache file, or ``None`` for "no cache" (``none``).

    Only meaningful when :func:`web_cache_requested` is true.
    """
    value = explicit if explicit is not None else os.environ.get(ENV_WEB_CACHE_DB, "")
    value = str(value).strip()
    if value.lower() in NO_CACHE_VALUES:
        return None
    if value.lower() in DEFAULT_VALUES:
        return default_web_cache_path()
    return os.path.expanduser(value)


def _canon(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _is_under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives / mixed absolute+relative
        return False


def production_db_paths(explicit: str | None = None) -> list[str]:
    """Every configured production crawl-store path (explicit one and the env one)."""
    found = [p for p in (explicit, os.environ.get(ENV_NEWS_DB)) if p]
    return list(dict.fromkeys(found))


def check_cache_path(cache_path: str, production_paths: list[str] | None = None) -> None:
    """Raise :class:`WebCachePathError` if ``cache_path`` is, or sits in, production."""
    target = _canon(cache_path)
    for prod in production_paths if production_paths is not None else production_db_paths():
        for suffix in _SIDE_SUFFIXES:
            other = prod + suffix
            same = target == _canon(other)
            if not same and os.path.exists(cache_path) and os.path.exists(other):
                try:
                    same = os.path.samefile(cache_path, other)
                except OSError:
                    same = False
            if same:
                raise WebCachePathError(
                    f"{ENV_WEB_CACHE_DB}={cache_path!r} resolves to the production crawl store "
                    f"({prod!r}); refusing to use it as the interactive web cache. Point "
                    f"{ENV_WEB_CACHE_DB} at a disposable file, or 'none'."
                )
    for root in PROTECTED_DIRS:
        if _is_under(target, _canon(root)):
            raise WebCachePathError(
                f"{ENV_WEB_CACHE_DB}={cache_path!r} lies under {root!r}, which holds the "
                f"production stores; refusing to write an interactive cache there. Point "
                f"{ENV_WEB_CACHE_DB} at a disposable file (default {default_web_cache_path()!r}) "
                f"or 'none'."
            )


# --------------------------------------------------------------------------- #
# Layered (read-only view over interactive + production)
# --------------------------------------------------------------------------- #
class LayeredReadDB:
    """The ``CrawlerDB`` read surface the cache tools use, over two stores.

    Deliberately exposes **no write method**: if ``CrawlerTools`` ever tried to
    write through ``tools.db`` it would fail with ``AttributeError`` instead of
    reaching either store (the crawler itself holds the interactive DB directly).
    """

    def __init__(self, interactive: Any, production: Any | None = None) -> None:
        self.interactive = interactive
        self.production = production

    @property
    def cfg(self) -> Any:
        return self.interactive.cfg

    def _prod(self, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        if self.production is None:
            return None
        try:
            return getattr(self.production, fn_name)(*args, **kwargs)
        except sqlite3.Error as exc:  # an unreadable production store must not break the tool
            _log.warning("production store read %s failed (%s); using interactive cache only", fn_name, exc)
            return None

    def get_page(self, uh: str) -> dict[str, Any] | None:
        return self.interactive.get_page(uh) or self._prod("get_page", uh)

    def get_pages(self, session_id: str | None = None, status: str | None = "done", limit: int = 0) -> list[dict[str, Any]]:
        rows = list(self.interactive.get_pages(session_id=session_id, status=status, limit=limit))
        if session_id and rows:
            return rows
        extra = list(self._prod("get_pages", session_id=session_id, status=status, limit=limit) or [])
        return extra if session_id else rows + extra

    def search_text(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for rows in (self.interactive.search_text(query, limit=limit), self._prod("search_text", query, limit=limit) or []):
            for row in rows:
                key = row.get("url_hash") or row.get("url")
                if key in seen:
                    continue
                seen.add(key)
                merged.append(row)
        return merged[: max(0, int(limit))]

    def get_crawl_graph(self, session_id: str, *, limit: int = 200) -> dict[str, Any]:
        graph = self.interactive.get_crawl_graph(session_id, limit=limit)
        if graph["nodes"]:
            return graph  # type: ignore[no-any-return]
        return self._prod("get_crawl_graph", session_id, limit=limit) or graph  # type: ignore[no-any-return]

    def get_artifacts(self, **kwargs: Any) -> list[dict[str, Any]]:
        rows = self.interactive.get_artifacts(**kwargs)
        if rows:
            return rows  # type: ignore[no-any-return]
        return self._prod("get_artifacts", **kwargs) or []  # type: ignore[no-any-return]

    def stats(self) -> dict[str, Any]:
        return {"interactive": self.interactive.stats(), "production": self._prod("stats")}

    def close(self) -> None:
        for db in (self.interactive, self.production):
            if db is not None:
                db.close()


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def build_interactive_crawler_tools(
    *,
    web_cache_db: str | None = None,
    news_db: str | None = None,
    **crawler_kwargs: Any,
) -> Any:
    """A ``lazycrawler.CrawlerTools`` that never writes to the production store.

    Fetch tools write to the interactive cache (``LAZYTOOLS_WEB_CACHE_DB``);
    read tools see that cache merged over the read-only production store.
    Raises :class:`WebCachePathError` before opening anything if the cache path
    aliases production.
    """
    from lazycrawler import CrawlerDB, CrawlerTools, DBConfig

    cache_path = resolve_web_cache_path(web_cache_db)
    prod_paths = production_db_paths(news_db)
    if cache_path is not None:
        check_cache_path(cache_path, prod_paths)
        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
    interactive = CrawlerDB(DBConfig(db_path=cache_path or ":memory:"))

    production = None
    prod_path = prod_paths[0] if prod_paths else None
    if prod_path:
        if os.path.isfile(prod_path):
            try:
                production = CrawlerDB(DBConfig(db_path=prod_path, read_only=True))
            except sqlite3.Error as exc:
                _log.warning("production store %s could not be opened read-only: %s", prod_path, exc)
        else:
            _log.warning("production store %s does not exist; read tools use the interactive cache only", prod_path)

    tools: Any = CrawlerTools(db=interactive, **crawler_kwargs)
    tools.db = LayeredReadDB(interactive, production)
    return tools
