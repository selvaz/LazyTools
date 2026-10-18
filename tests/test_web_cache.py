"""Interactive web-cache isolation (``LAZYTOOLS_WEB_CACHE_DB``) for the MCP ``web`` provider.

No network: the crawler's HTTP fetch, DuckDuckGo lookup, robots and SSRF checks
are patched. The "production" store is a throw-away SQLite file built per test.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lazycrawler")
pytest.importorskip("lazybridge")

from lazycrawler import CrawlerDB, DBConfig
from lazycrawler.http import url_hash

from lazytools.connectors.web import cache as wc
from lazytools.connectors.web.cache import (
    ENV_WEB_CACHE_DB,
    WebCachePathError,
    build_interactive_crawler_tools,
    check_cache_path,
    resolve_web_cache_path,
    web_cache_requested,
)
from lazytools.mcp_server.providers import PROVIDER_FACTORIES, default_providers

PROD_URL = "https://prod.example.test/committee-story"
BODY = "Interactive research page about lithium supply chains and battery metals. " * 12


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_WEB_CACHE_DB, raising=False)
    monkeypatch.delenv("LAZYCRAWLER_NEWS_DB", raising=False)
    monkeypatch.delenv("LAZYTOOLS_DATA_DIR", raising=False)


@pytest.fixture
def fake_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch every network touchpoint; returns the list of fetched URLs."""
    import lazycrawler.crawler as crawler_mod
    import lazycrawler.http as http_mod
    import lazycrawler.search as search_mod

    fetched: list[str] = []

    def fake_fetch(self: Any, url: str, extra_headers: Any = None) -> Any:
        fetched.append(url)
        html = f"<html><head><title>T {url}</title></head><body><p>{BODY}</p></body></html>"
        return http_mod.FetchResult(
            html=html, text=BODY, status=200, content_type="text/html", final_url=url
        )

    monkeypatch.setattr(http_mod.HTTPClient, "fetch", fake_fetch)
    monkeypatch.setattr(http_mod.RobotsChecker, "allowed", lambda self, url: True)
    monkeypatch.setattr(http_mod.RobotsChecker, "crawl_delay", lambda self, url: None)
    monkeypatch.setattr(crawler_mod, "is_blocked_address", lambda url: False)
    monkeypatch.setattr(
        search_mod,
        "search_ddg_urls",
        lambda query, n, blacklist=None, **kw: ["https://research.example.test/a"],
    )
    return fetched


def _make_production(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = CrawlerDB(DBConfig(db_path=str(path)))
    db.create_session("news_prod_1", topic="rates", seed=PROD_URL, mode="pure", source="crawl")
    uh = db.upsert_page(
        {
            "url": PROD_URL,
            "status": "done",
            "mode": "pure",
            "title": "Committee story on interest rates",
            "clean_text": "The central bank held interest rates steady amid sticky inflation.",
        }
    )
    db.add_edge("news_prod_1", uh, depth=0)
    db.close()
    return path


def _fingerprint(path: Path) -> tuple[str, int, int]:
    st = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), st.st_mtime_ns, st.st_size


@pytest.fixture
def prod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _make_production(tmp_path / "prod" / "news.db")
    monkeypatch.setenv("LAZYCRAWLER_NEWS_DB", str(path))
    return path


def _tools(cache: Path | str, prod_path: Path | None = None) -> Any:
    return build_interactive_crawler_tools(
        web_cache_db=str(cache), news_db=str(prod_path) if prod_path else None, content="pure"
    )


# --------------------------------------------------------------------------- #
# Opt-in: unset == today's behaviour (regression for the scheduled agents)
# --------------------------------------------------------------------------- #
def test_unset_keeps_production_write_path(prod: Path, fake_network: list[str]) -> None:
    assert web_cache_requested() is False
    web = PROVIDER_FACTORIES["web"](False)
    crawler = web._provider
    assert type(crawler.db).__name__ == "CrawlerDB"  # not the layered read view
    assert crawler.db.cfg.db_path == str(prod)
    assert crawler.db.cfg.read_only is False
    crawler.content = "pure"

    out = json.loads(crawler.web_crawl("https://agent.example.test/page", depth=0))
    assert out["found"] == 1
    crawler.db.close()

    check = CrawlerDB(DBConfig(db_path=str(prod), read_only=True))
    try:
        assert check.get_page(url_hash("https://agent.example.test/page")) is not None
        assert check.get_pages(session_id=out["session_id"])  # session landed in production
    finally:
        check.close()


def test_opt_in_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    assert web_cache_requested() is False  # unset: legacy behaviour
    assert web_cache_requested("") is True  # an explicit empty value opts in (default path)
    monkeypatch.setenv(ENV_WEB_CACHE_DB, "none")
    assert web_cache_requested() is True


# --------------------------------------------------------------------------- #
# Isolation when set
# --------------------------------------------------------------------------- #
def test_search_then_crawl_leave_production_byte_identical(prod: Path, tmp_path: Path, fake_network: list[str]) -> None:
    before = _fingerprint(prod)
    cache = tmp_path / "interactive" / "web_cache.sqlite"
    tools = _tools(cache, prod)

    search = json.loads(tools.web_search("lithium", max_results=1))
    crawl = json.loads(tools.web_crawl("https://other.example.test/b", depth=0))
    many = json.loads(tools.web_crawl_many(["https://other.example.test/c"], depth=0))
    assert search["found"] == 1 and crawl["found"] == 1 and many["found"] == 1
    # read tools over both stores, too
    tools.search_cached("interest rates")
    tools.get_page(PROD_URL)
    tools.get_cache_stats()
    tools.db.close()

    assert _fingerprint(prod) == before  # hash, mtime, size
    with CrawlerDB(DBConfig(db_path=str(prod), read_only=True)) as db:
        assert db.stats()["sessions"] == 1  # only the committee's own session
        assert db.get_page(url_hash("https://other.example.test/b")) is None


def test_cache_created_at_configured_path_and_holds_interactive_pages(prod: Path, tmp_path: Path, fake_network: list[str]) -> None:
    cache = tmp_path / "deep" / "dir" / "my_cache.sqlite"
    assert not cache.exists()
    tools = _tools(cache, prod)
    assert cache.is_file()
    out = json.loads(tools.web_crawl("https://other.example.test/b", depth=0))
    tools.db.close()
    with CrawlerDB(DBConfig(db_path=str(cache), read_only=True)) as db:
        assert db.get_page(url_hash("https://other.example.test/b")) is not None
        assert db.get_pages(session_id=out["session_id"])
        assert db.get_page(url_hash(PROD_URL)) is None  # nothing copied from production


def test_none_keeps_nothing_on_disk(prod: Path, tmp_path: Path, fake_network: list[str]) -> None:
    before = _fingerprint(prod)
    work = tmp_path / "cwd"
    work.mkdir()
    snapshot = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    tools = build_interactive_crawler_tools(web_cache_db="none", content="pure")
    out = json.loads(tools.web_crawl("https://other.example.test/b", depth=0))
    assert out["found"] == 1
    # usable within the process...
    assert json.loads(tools.get_page("https://other.example.test/b"))["url"].endswith("/b")
    tools.db.close()
    # ...but no file anywhere
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == snapshot
    assert _fingerprint(prod) == before
    assert resolve_web_cache_path("none") is None
    assert resolve_web_cache_path(":memory:") is None


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
def test_guard_refuses_production_file_and_sidecars(prod: Path) -> None:
    for candidate in (prod, Path(str(prod) + "-wal"), Path(str(prod) + "-shm")):
        with pytest.raises(WebCachePathError, match="production"):
            build_interactive_crawler_tools(web_cache_db=str(candidate), content="pure")
    # different spelling of the same file
    with pytest.raises(WebCachePathError):
        build_interactive_crawler_tools(web_cache_db=str(prod.parent / ".." / "prod" / "news.db"), content="pure")


def test_guard_refuses_explicit_news_db_not_in_env(tmp_path: Path) -> None:
    other = _make_production(tmp_path / "other_news.db")
    with pytest.raises(WebCachePathError):
        build_interactive_crawler_tools(web_cache_db=str(other), news_db=str(other), content="pure")


def test_guard_refuses_protected_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    protected = tmp_path / "InvestmentCommittee"
    monkeypatch.setattr(wc, "PROTECTED_DIRS", (str(protected),))
    target = protected / "live" / "db" / "web_cache.sqlite"
    with pytest.raises(WebCachePathError, match="InvestmentCommittee"):
        build_interactive_crawler_tools(web_cache_db=str(target), content="pure")
    assert not protected.exists()  # refused before anything was created
    check_cache_path(str(tmp_path / "InvestmentCommittee_sibling" / "x.sqlite"), [])  # not "under"


@pytest.mark.skipif(sys.platform != "win32", reason="literal Windows production path")
def test_guard_default_protected_dir_is_programdata_investmentcommittee() -> None:
    with pytest.raises(WebCachePathError):
        check_cache_path(r"C:\ProgramData\InvestmentCommittee\live\db\web_cache.sqlite", [])
    with pytest.raises(WebCachePathError):
        check_cache_path(r"c:\programdata\investmentcommittee\anything.sqlite", [])


def test_mcp_provider_raises_instead_of_skipping(prod: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_WEB_CACHE_DB, str(prod))
    before = _fingerprint(prod)
    with pytest.raises(WebCachePathError):
        default_providers(["web"])
    assert _fingerprint(prod) == before


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def test_default_path_is_under_home_lazytools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    expected = os.path.join(os.path.expanduser("~"), ".lazytools", "web_cache.sqlite")
    assert resolve_web_cache_path("") == expected
    assert resolve_web_cache_path("default") == expected
    monkeypatch.setenv(ENV_WEB_CACHE_DB, "")
    assert web_cache_requested() is True
    assert resolve_web_cache_path() == expected
    monkeypatch.setenv("LAZYTOOLS_DATA_DIR", str(tmp_path / "data"))
    assert resolve_web_cache_path() == str(tmp_path / "data" / "web_cache.sqlite")


def test_provider_uses_env_and_layers(prod: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "env_cache.sqlite"
    monkeypatch.setenv(ENV_WEB_CACHE_DB, str(cache))
    web = PROVIDER_FACTORIES["web"](False)
    assert cache.is_file()
    assert isinstance(web._provider.db, wc.LayeredReadDB)
    assert web._provider.db.production is not None
    assert web._provider.db.production.cfg.read_only is True
    names = {t.name for t in web.as_tools()}
    assert {"web_search", "web_crawl", "web_crawl_many", "search_cached", "get_page"} <= names


# --------------------------------------------------------------------------- #
# Read tools still see production
# --------------------------------------------------------------------------- #
def test_read_tools_return_production_pages(prod: Path, tmp_path: Path, fake_network: list[str]) -> None:
    before = _fingerprint(prod)
    tools = _tools(tmp_path / "c.sqlite", prod)

    found = json.loads(tools.search_cached("interest rates"))
    assert [p["url"] for p in found["pages"]] == [PROD_URL]

    page = json.loads(tools.get_page(PROD_URL))
    assert page["title"] == "Committee story on interest rates"
    assert "sticky inflation" in page["untrusted_page_text"]

    sess = json.loads(tools.get_session_pages("news_prod_1"))
    assert sess["found"] == 1
    graph = json.loads(tools.get_crawl_graph("news_prod_1"))
    assert [n["url"] for n in graph["nodes"]] == [PROD_URL]

    stats = json.loads(tools.get_cache_stats())
    assert stats["production"]["pages"] == 1
    assert stats["interactive"]["pages"] == 0

    # an interactive page appears next to it
    json.loads(tools.web_crawl("https://other.example.test/b", depth=0))
    both = json.loads(tools.search_cached("lithium OR rates"))
    urls = {p["url"] for p in both["pages"]}
    assert "https://other.example.test/b" in urls
    stats = json.loads(tools.get_cache_stats())
    assert stats["interactive"]["pages"] == 1 and stats["production"]["pages"] == 1
    tools.db.close()
    assert _fingerprint(prod) == before


def test_layered_view_exposes_no_write_methods(prod: Path, tmp_path: Path) -> None:
    tools = _tools(tmp_path / "c.sqlite", prod)
    for name in ("upsert_page", "create_session", "add_edge", "add_artifacts", "conn"):
        assert not hasattr(tools.db, name)
    tools.db.close()


def test_missing_production_file_degrades_to_interactive_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_network: list[str]) -> None:
    missing = tmp_path / "nope" / "news.db"
    monkeypatch.setenv("LAZYCRAWLER_NEWS_DB", str(missing))
    tools = _tools(tmp_path / "c.sqlite")
    assert tools.db.production is None
    assert json.loads(tools.search_cached("anything"))["found"] == 0
    assert not missing.parent.exists()  # not created
    tools.db.close()


# --------------------------------------------------------------------------- #
# Round 1: Windows namespace spellings of a protected path
# --------------------------------------------------------------------------- #
_WIN = pytest.mark.skipif(sys.platform != "win32", reason="Windows path namespaces")
_PROTECTED_TAIL = r"ProgramData\InvestmentCommittee\live\db\review-cache.sqlite"
_HOST = os.environ.get("COMPUTERNAME", "SOMEHOST")


@_WIN
@pytest.mark.parametrize(
    "spelling",
    [
        r"\\?\C:\ProgramData\InvestmentCommittee\live\db\review-cache.sqlite",
        r"\\?\c:\PROGRAMDATA\investmentcommittee\live\db\review-cache.sqlite",
        r"\\.\C:\ProgramData\InvestmentCommittee\live\db\review-cache.sqlite",
        "//?/C:/ProgramData/InvestmentCommittee/live/db/review-cache.sqlite",
        r"\\?\C:\ProgramData\InvestmentCommittee\..\InvestmentCommittee\live\review-cache.sqlite",
        # UNC forms of this machine's C: drive
        r"\\?\UNC\localhost\C$\ProgramData\InvestmentCommittee\live\db\review-cache.sqlite",
        r"\\localhost\C$\ProgramData\InvestmentCommittee\live\db\review-cache.sqlite",
        r"\\127.0.0.1\c$\programdata\investmentcommittee\live\db\review-cache.sqlite",
        rf"\\?\UNC\{_HOST}\C$\ProgramData\InvestmentCommittee\live\db\review-cache.sqlite",
    ],
)
def test_guard_refuses_extended_and_unc_spellings_of_protected_dir(spelling: str) -> None:
    with pytest.raises(WebCachePathError, match="InvestmentCommittee"):
        check_cache_path(spelling, [])


@_WIN
@pytest.mark.parametrize(
    "spelling",
    [
        r"\\?\C:\Users\someone\.lazytools\web_cache.sqlite",
        r"\\?\UNC\fileserver\share\web_cache.sqlite",
        r"\\fileserver\share\InvestmentCommittee\web_cache.sqlite",  # another machine's share
        r"\\localhost\D$\scratch\web_cache.sqlite",  # another drive
    ],
)
def test_guard_allows_unrelated_extended_and_unc_paths(spelling: str) -> None:
    check_cache_path(spelling, [])


@_WIN
def test_extended_prefix_cannot_create_cache_inside_protected_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    protected = tmp_path / "InvestmentCommittee"
    monkeypatch.setattr(wc, "PROTECTED_DIRS", (str(protected),))
    target = "\\\\?\\" + str(protected / "live" / "db" / "review-cache.sqlite")
    with pytest.raises(WebCachePathError):
        build_interactive_crawler_tools(web_cache_db=target, content="pure")
    assert not protected.exists()  # refused before anything was created


@_WIN
def test_extended_prefix_spelling_of_production_file_is_refused(prod: Path) -> None:
    for spelling in ("\\\\?\\" + str(prod), "\\\\?\\" + str(prod) + "-wal", str(prod).upper()):
        with pytest.raises(WebCachePathError, match="production"):
            build_interactive_crawler_tools(web_cache_db=spelling, content="pure")


# --------------------------------------------------------------------------- #
# Round 1: a page and its artifacts come from one store only
# --------------------------------------------------------------------------- #
OLD_TABLE = "OLD_PRODUCTION_TABLE_VALUE=123"
NEW_TEXT = "NEW_INTERACTIVE_NARRATIVE"


def _production_with_table(path: Path) -> Path:
    _make_production(path)
    db = CrawlerDB(DBConfig(db_path=str(path)))
    db.add_artifacts(
        url_hash(PROD_URL),
        [{"artifact_type": "table", "content": OLD_TABLE, "content_hash": "tbl1", "position": 0}],
    )
    db.close()
    return path


def _interactive_copy_without_artifacts(cache: Path) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    db = CrawlerDB(DBConfig(db_path=str(cache)))
    db.create_session("interactive_1", seed=PROD_URL)
    uh = db.upsert_page(
        {"url": PROD_URL, "status": "done", "mode": "pure", "title": "Newer copy", "clean_text": NEW_TEXT}
    )
    db.add_edge("interactive_1", uh, depth=0)
    db.close()


def test_interactive_page_never_inherits_production_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prod_path = _production_with_table(tmp_path / "prod" / "news.db")
    monkeypatch.setenv("LAZYCRAWLER_NEWS_DB", str(prod_path))
    cache = tmp_path / "c.sqlite"
    _interactive_copy_without_artifacts(cache)
    before = _fingerprint(prod_path)
    tools = _tools(cache, prod_path)

    rag = json.loads(tools.get_rag_document(PROD_URL))["rag_document"]
    assert NEW_TEXT in rag
    assert OLD_TABLE not in rag  # the review scenario: new narrative + stale table

    by_url = json.loads(tools.get_artifacts(url=PROD_URL))
    assert by_url["found"] == 0  # legitimately empty, not a fallback to production
    assert json.loads(tools.get_artifacts(url=PROD_URL, artifact_type="table"))["found"] == 0
    assert json.loads(tools.get_artifacts(session_id="interactive_1"))["found"] == 0
    assert json.loads(tools.get_artifacts(session_id="interactive_1", artifact_type="table"))["found"] == 0
    tools.db.close()
    assert _fingerprint(prod_path) == before


def test_production_only_page_keeps_its_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prod_path = _production_with_table(tmp_path / "prod" / "news.db")
    monkeypatch.setenv("LAZYCRAWLER_NEWS_DB", str(prod_path))
    tools = _tools(tmp_path / "c.sqlite", prod_path)  # interactive cache has never seen the URL

    rag = json.loads(tools.get_rag_document(PROD_URL))["rag_document"]
    assert OLD_TABLE in rag
    found = json.loads(tools.get_artifacts(url=PROD_URL, artifact_type="table"))
    assert found["found"] == 1
    assert json.loads(tools.get_artifacts(url=PROD_URL, artifact_type="image"))["found"] == 0
    assert json.loads(tools.get_artifacts(session_id="news_prod_1"))["found"] == 1
    tools.db.close()


def test_interactive_artifacts_win_when_the_interactive_copy_has_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prod_path = _production_with_table(tmp_path / "prod" / "news.db")
    monkeypatch.setenv("LAZYCRAWLER_NEWS_DB", str(prod_path))
    cache = tmp_path / "c.sqlite"
    _interactive_copy_without_artifacts(cache)
    db = CrawlerDB(DBConfig(db_path=str(cache)))
    db.add_artifacts(url_hash(PROD_URL), [{"artifact_type": "table", "content": "NEW_TABLE", "content_hash": "t2"}])
    db.close()
    tools = _tools(cache, prod_path)
    rag = json.loads(tools.get_rag_document(PROD_URL))["rag_document"]
    assert "NEW_TABLE" in rag and OLD_TABLE not in rag
    assert json.loads(tools.get_artifacts(url=PROD_URL))["found"] == 1
    tools.db.close()
