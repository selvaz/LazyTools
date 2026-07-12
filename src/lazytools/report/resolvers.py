"""Source-scheme artifact resolvers: regimes, crawler, chart.

Factories building :class:`~lazytools.report.artifacts.Resolver` callables
for the ecosystem's artifact schemes. Every producer package is imported
*lazily inside the resolver* — constructing a registry never imports
lazystats / lazycrawler / market-data-hub; a missing package raises a clear
ImportError with the install hint only when a ref of that scheme is actually
resolved. Each factory also accepts an already-open, duck-typed store object
so callers (and tests) can inject one.

:func:`ecosystem_resolvers` is the one-call assembly used by scheduled
scripts and by :class:`~lazytools.report.tools.ReportTools` wiring.
"""

from __future__ import annotations

from typing import Any

from lazytools.report.artifacts import ArtifactResolvers, Resolver, sniff_image_mime


def regimes_resolver(db: Any = None) -> Resolver:
    """Resolve ``regimes:<plot_key>`` to the PNG stored in a LazyStats depot.

    ``db`` may be: ``None`` (use the session depot initialised via
    ``lazystats.regimes.db.init_regime_db``), a path to the depot SQLite
    file, or any object with ``get_plot(plot_key) -> bytes``.
    """

    def _resolve(key: str) -> tuple[bytes, str]:
        store = db
        if store is None or isinstance(store, str):
            try:
                import lazystats.regimes.db as _rdb
            except ImportError as exc:  # pragma: no cover - needs the extra absent
                raise ImportError(
                    "resolving 'regimes:' artifacts requires lazystats[regimes]: "
                    "pip install 'lazystats[regimes] @ "
                    "git+https://github.com/selvaz/LazyStats.git'"
                ) from exc
            store = _rdb.RegimeDB(store) if isinstance(store, str) else _rdb.get_db()
        return store.get_plot(key), "image/png"

    return _resolve


def crawler_resolver(db: Any) -> Resolver:
    """Resolve ``crawler:<content_hash>`` to the blob in a LazyCrawler DB.

    ``db`` may be a path to the crawler SQLite file or any object with
    ``get_artifacts(content_hash=..., include_blob=True)`` (a
    ``lazycrawler.CrawlerDB``). Only artifacts whose bytes were downloaded
    (``download_artifact_bytes=True`` at crawl time) are resolvable.

    A ``content_hash`` is unique only per page, so the same image on several
    crawled pages matches multiple rows — some possibly without a downloaded
    blob. All rows share the same underlying content, so any one carrying
    bytes is correct; this picks the first such row rather than a blind
    ``limit=1`` that could land on a bytesless duplicate.
    """

    def _resolve(key: str) -> tuple[bytes, str]:
        store = db
        if isinstance(store, str):
            try:
                from lazycrawler import CrawlerDB, DBConfig
            except ImportError as exc:  # pragma: no cover - needs the extra absent
                raise ImportError(
                    "resolving 'crawler:' artifacts requires lazycrawler: "
                    "pip install 'lazycrawler @ "
                    "git+https://github.com/selvaz/LazyCrawler.git'"
                ) from exc
            store = CrawlerDB(DBConfig(db_path=store))
        rows = store.get_artifacts(content_hash=key, include_blob=True)
        if not rows:
            raise KeyError(f"crawler artifact {key!r} not found")
        row = next((r for r in rows if r.get("blob")), None)
        if row is None:
            raise ValueError(
                f"crawler artifact {key!r} has no stored bytes on any page "
                "(crawl with download_artifact_bytes=True to persist them)"
            )
        data = bytes(row["blob"])
        return data, row.get("mime") or sniff_image_mime(data)

    return _resolve


def chart_resolver(db_path: str | None = None) -> Resolver:
    """Resolve ``chart:<spec>`` by rendering a datahub series chart on demand.

    The key is a querystring spec (see
    :func:`lazytools.report.charts.parse_chart_spec`), e.g.
    ``chart:symbols=SPY,^VIX&start=2020-01-01&frequency=W&transform=log_return``.
    Rendering happens at resolve time against the hub's DuckDB — output is
    deterministic given the same stored data.
    """

    def _resolve(key: str) -> tuple[bytes, str]:
        from lazytools.report.charts import chart_series, parse_chart_spec

        spec = parse_chart_spec(key)
        return chart_series(db_path=db_path, **spec), "image/png"

    return _resolve


def ecosystem_resolvers(
    *,
    regimes_db: Any = None,
    crawler_db: Any = None,
    datahub_db_path: str | None = None,
    file_base_dir: str | None = None,
) -> ArtifactResolvers:
    """A registry with every ecosystem scheme registered.

    ``regimes:`` and ``chart:`` are always registered (they fail with the
    install hint only if actually used without their package); ``crawler:``
    only when ``crawler_db`` is given, since it has no session default.
    """
    resolvers = ArtifactResolvers(file_base_dir=file_base_dir)
    resolvers.register("regimes", regimes_resolver(regimes_db))
    resolvers.register("chart", chart_resolver(datahub_db_path))
    if crawler_db is not None:
        resolvers.register("crawler", crawler_resolver(crawler_db))
    return resolvers
