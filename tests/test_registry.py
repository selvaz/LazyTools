"""DB registry (env-var resolution) + artifact catalog (SQLite) + router fan-out."""

from __future__ import annotations

import hashlib

import pytest

from lazytools.registry import db
from lazytools.registry.artifacts import get_artifact, register_artifact, search_artifacts
from lazytools.registry.router import get_everywhere, search_everywhere


@pytest.fixture(autouse=True)
def _clear_artifact_db_env_vars(monkeypatch):
    """Every test here that inspects artifact_dbs()/fan-out behavior must
    start from a clean slate -- an ambient env var (from the real
    deployment, or left set by another test) would silently add an
    unexpected entry and break assertions about exactly which DBs are
    "configured". Derived from KNOWN_DBS itself so a newly added
    *_artifacts entry is automatically covered here, never requiring this
    fixture to be updated by hand. Tests that want a specific one active
    still `monkeypatch.setenv(...)` it themselves, which runs after (and so
    overrides) this autouse clear."""
    for entry in db.KNOWN_DBS:
        if entry.name.endswith("_artifacts"):
            monkeypatch.delenv(entry.env_var, raising=False)


# --------------------------------------------------------------------------- #
# db.resolve_db / db.status / db.artifact_dbs
# --------------------------------------------------------------------------- #
def test_resolve_db_unknown_name_raises_key_error() -> None:
    with pytest.raises(KeyError):
        db.resolve_db("not_a_real_db")


def test_resolve_db_required_and_unset_raises_runtime_error_naming_env_var(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_DATA_DB", raising=False)
    with pytest.raises(RuntimeError, match="MARKET_DATA_DB"):
        db.resolve_db("market_data")


def test_resolve_db_not_required_and_unset_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_DATA_ARTIFACTS_DB", raising=False)
    assert db.resolve_db("market_data_artifacts") is None


def test_resolve_db_set_returns_env_value(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_DATA_DB", "/tmp/market.duckdb")
    assert db.resolve_db("market_data") == "/tmp/market.duckdb"


def test_status_reflects_set_env_vars(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_DATA_DB", raising=False)
    monkeypatch.setenv("STORE_DB", "/tmp/pulse.db")
    monkeypatch.delenv("LAZYCRAWLER_NEWS_DB", raising=False)

    rows = {row["name"]: row for row in db.status()}
    assert rows["market_data"]["set"] is False
    assert rows["market_data"]["required"] is True
    assert rows["market_data"]["env_var"] == "MARKET_DATA_DB"
    assert rows["market_data"]["owner_repo"] == "market-data-hub"
    assert rows["pulse_state"]["set"] is True
    assert rows["crawler_raw"]["set"] is False
    assert len(db.status()) == len(db.KNOWN_DBS)


def test_artifact_dbs_only_lists_set_artifact_entries(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_DATA_ARTIFACTS_DB", raising=False)
    monkeypatch.setenv("PULSE_ARTIFACTS_DB", "/tmp/pulse_artifacts.db")
    monkeypatch.delenv("CRAWLER_ARTIFACTS_DB", raising=False)

    entries = db.artifact_dbs()
    assert ("lazypulse", "/tmp/pulse_artifacts.db") in entries
    assert not any(repo == "market-data-hub" for repo, _ in entries)
    assert not any(repo == "lazycrawler" for repo, _ in entries)
    # Non-artifact DBs (market_data, pulse_state, crawler_raw) never appear here.
    assert len(entries) <= 1


# --------------------------------------------------------------------------- #
# artifacts.register_artifact / search_artifacts / get_artifact
# --------------------------------------------------------------------------- #
def test_register_artifact_creates_table_and_returns_id(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    artifact_id = register_artifact(
        db_path, repo="lazytools", kind="report", title="t", summary="s",
    )
    assert isinstance(artifact_id, str) and artifact_id

    record = get_artifact(db_path, artifact_id)
    assert record is not None
    assert record["repo"] == "lazytools"
    assert record["kind"] == "report"
    assert record["title"] == "t"
    assert record["summary"] == "s"
    assert record["tags"] == []
    assert record["content"] is None
    assert record["content_hash"] is None
    assert record["expires_at"] is None


def test_register_artifact_stores_content_hash_and_tags(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    artifact_id = register_artifact(
        db_path, repo="lazytools", kind="report", title="t", summary="s",
        tags=["alpha", "beta"], content="hello world",
    )
    record = get_artifact(db_path, artifact_id)
    assert record["tags"] == ["alpha", "beta"]
    assert record["content"] == "hello world"
    assert record["content_hash"] == hashlib.sha256(b"hello world").hexdigest()


def test_search_artifacts_never_returns_content(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(
        db_path, repo="lazytools", kind="report", title="t", summary="s", content="SECRET PAYLOAD",
    )
    results = search_artifacts(db_path)
    assert len(results) == 1
    assert "content" not in results[0]
    assert "content_uri" not in results[0]
    assert "content_hash" not in results[0]


def test_search_artifacts_filters_by_kind(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(db_path, repo="r", kind="backtest", title="a", summary="s")
    register_artifact(db_path, repo="r", kind="regime", title="b", summary="s")

    results = search_artifacts(db_path, kind="backtest")
    assert len(results) == 1
    assert results[0]["title"] == "a"


def test_search_artifacts_filters_by_query_on_title_summary_tags(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(db_path, repo="r", kind="k", title="SPY momentum", summary="s", tags=["equities"])
    register_artifact(db_path, repo="r", kind="k", title="unrelated", summary="bond yields", tags=["fixed_income"])

    assert {r["title"] for r in search_artifacts(db_path, query="SPY")} == {"SPY momentum"}
    assert {r["title"] for r in search_artifacts(db_path, query="yields")} == {"unrelated"}
    assert {r["title"] for r in search_artifacts(db_path, query="equities")} == {"SPY momentum"}


def test_search_artifacts_escapes_like_wildcards_in_query(tmp_path) -> None:
    """A literal "%" or "_" in the search text must be matched literally,
    not interpreted as a SQL LIKE wildcard."""
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(db_path, repo="r", kind="k", title="5% return", summary="s")
    register_artifact(db_path, repo="r", kind="k", title="unrelated", summary="s")

    results = search_artifacts(db_path, query="5% return")
    assert {r["title"] for r in results} == {"5% return"}

    # "_" is also a LIKE wildcard (matches any single char) -- must be literal too.
    register_artifact(db_path, repo="r", kind="k", title="a_b special", summary="s")
    register_artifact(db_path, repo="r", kind="k", title="axb should not match", summary="s")
    results = search_artifacts(db_path, query="a_b")
    assert {r["title"] for r in results} == {"a_b special"}


def test_search_artifacts_query_matches_decoded_tag_values(tmp_path) -> None:
    """A query must match a tag's actual text, not its json.dumps() bytes --
    non-ASCII characters and quotes are escaped in the serialized column
    (e.g. "café" -> "caf\\u00e9"), so a literal-text query against that
    serialized form would never find an exact tag match."""
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(db_path, repo="r", kind="k", title="t", summary="s", tags=["café"])
    register_artifact(db_path, repo="r", kind="k", title="unrelated", summary="s", tags=["other"])

    results = search_artifacts(db_path, query="café")
    assert {r["title"] for r in results} == {"t"}


def test_search_artifacts_filters_by_tags_all_must_be_present(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(db_path, repo="r", kind="k", title="a", summary="s", tags=["x", "y"])
    register_artifact(db_path, repo="r", kind="k", title="b", summary="s", tags=["x"])

    results = search_artifacts(db_path, tags=["x", "y"])
    assert {r["title"] for r in results} == {"a"}


def test_search_artifacts_respects_limit_with_tags_filter(tmp_path) -> None:
    """limit must still be honored when a Python-side tags filter is
    involved, even when most rows don't match the tags (i.e. more rows
    exist than would satisfy `limit` on the first SQL page)."""
    db_path = str(tmp_path / "artifacts.db")
    for i in range(30):
        register_artifact(db_path, repo="r", kind="k", title=f"noise-{i}", summary="s", tags=["other"])
    for i in range(3):
        register_artifact(db_path, repo="r", kind="k", title=f"match-{i}", summary="s", tags=["wanted"])

    results = search_artifacts(db_path, tags=["wanted"], limit=2)
    assert len(results) == 2
    assert all("wanted" in r["tags"] for r in results)


def test_search_artifacts_filters_by_since(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(db_path, repo="r", kind="k", title="old", summary="s")

    future = "2999-01-01T00:00:00+00:00"
    assert search_artifacts(db_path, since=future) == []
    assert len(search_artifacts(db_path, since="2000-01-01T00:00:00+00:00")) == 1


def test_search_artifacts_filters_by_since_normalizes_non_utc_offset(tmp_path) -> None:
    """A `since` with a non-UTC offset must compare as the same instant as
    created_at (always stored in UTC), not as mismatched string spellings.

    A naive string comparison would put a "+02:00"-offset timestamp *after*
    a same-instant (or even later) UTC one, because the local hour is 2
    higher than the UTC hour for the same moment -- e.g. 10:00 UTC is
    12:00+02:00, and "12:00...+02:00" > "10:00...+00:00" as raw strings
    despite being the same instant.
    """
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(db_path, repo="r", kind="k", title="recent", summary="s")

    from datetime import UTC, datetime, timedelta, timezone

    # An instant 5 seconds before "now", expressed in the +02:00 zone --
    # still <= created_at (which is ~now, in UTC), but its raw string sorts
    # later than a UTC "now" timestamp would.
    since_instant = datetime.now(UTC) - timedelta(seconds=5)
    since = since_instant.astimezone(timezone(timedelta(hours=2))).isoformat()

    results = search_artifacts(db_path, since=since)
    assert {r["title"] for r in results} == {"recent"}


def test_search_artifacts_respects_limit_and_orders_created_at_desc(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    for i in range(5):
        register_artifact(db_path, repo="r", kind="k", title=f"item-{i}", summary="s")

    results = search_artifacts(db_path, limit=2)
    assert len(results) == 2
    # Most-recently-inserted first.
    assert results[0]["title"] == "item-4"
    assert results[1]["title"] == "item-3"


def test_search_artifacts_rejects_non_positive_limit(tmp_path) -> None:
    """limit<=0 previously behaved inconsistently: the no-filter branch
    silently clamped it up to 1 result, while a query/tags-filtered branch
    returned zero or applied Python's negative-slice semantics. Both must
    now raise instead."""
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(db_path, repo="r", kind="k", title="a", summary="s")

    for bad_limit in (0, -1):
        with pytest.raises(ValueError, match="limit"):
            search_artifacts(db_path, limit=bad_limit)
        with pytest.raises(ValueError, match="limit"):
            search_artifacts(db_path, tags=["x"], limit=bad_limit)


def test_search_artifacts_excludes_expired(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    expired_id = register_artifact(
        db_path, repo="r", kind="k", title="expired", summary="s", ttl_days=-1,
    )
    live_id = register_artifact(db_path, repo="r", kind="k", title="live", summary="s", ttl_days=30)

    titles = {r["title"] for r in search_artifacts(db_path)}
    assert titles == {"live"}
    assert get_artifact(db_path, expired_id) is None
    assert get_artifact(db_path, live_id) is not None


def test_get_artifact_returns_none_for_nonexistent_id(tmp_path) -> None:
    db_path = str(tmp_path / "artifacts.db")
    register_artifact(db_path, repo="r", kind="k", title="t", summary="s")
    assert get_artifact(db_path, "does-not-exist") is None


# --------------------------------------------------------------------------- #
# router.search_everywhere / get_everywhere
# --------------------------------------------------------------------------- #
def test_search_everywhere_merges_and_sorts_across_repos(tmp_path, monkeypatch) -> None:
    hub_db = str(tmp_path / "hub_artifacts.db")
    pulse_db = str(tmp_path / "pulse_artifacts.db")
    crawler_db = str(tmp_path / "crawler_artifacts.db")

    monkeypatch.setenv("MARKET_DATA_ARTIFACTS_DB", hub_db)
    monkeypatch.setenv("PULSE_ARTIFACTS_DB", pulse_db)
    monkeypatch.delenv("CRAWLER_ARTIFACTS_DB", raising=False)

    register_artifact(hub_db, repo="market-data-hub", kind="k", title="hub-1", summary="s")
    register_artifact(pulse_db, repo="lazypulse", kind="k", title="pulse-1", summary="s")
    register_artifact(pulse_db, repo="lazypulse", kind="k", title="pulse-2", summary="s")
    # Crawler DB is unset -- should be skipped entirely, not error.
    register_artifact(crawler_db, repo="lazycrawler", kind="k", title="crawler-1", summary="s")

    results = search_everywhere()
    titles = [r["title"] for r in results]
    assert "crawler-1" not in titles
    assert set(titles) == {"hub-1", "pulse-1", "pulse-2"}

    repos = {r["title"]: r["repo"] for r in results}
    assert repos["hub-1"] == "market-data-hub"
    assert repos["pulse-1"] == "lazypulse"

    # Sorted created_at DESC across the merged set.
    created = [r["created_at"] for r in results]
    assert created == sorted(created, reverse=True)


def test_search_everywhere_respects_limit_after_merge(tmp_path, monkeypatch) -> None:
    hub_db = str(tmp_path / "hub_artifacts.db")
    pulse_db = str(tmp_path / "pulse_artifacts.db")
    monkeypatch.setenv("MARKET_DATA_ARTIFACTS_DB", hub_db)
    monkeypatch.setenv("PULSE_ARTIFACTS_DB", pulse_db)
    monkeypatch.delenv("CRAWLER_ARTIFACTS_DB", raising=False)

    for i in range(3):
        register_artifact(hub_db, repo="market-data-hub", kind="k", title=f"hub-{i}", summary="s")
    for i in range(3):
        register_artifact(pulse_db, repo="lazypulse", kind="k", title=f"pulse-{i}", summary="s")

    results = search_everywhere(limit=2)
    assert len(results) == 2


def test_get_everywhere_resolves_repo_then_fetches(tmp_path, monkeypatch) -> None:
    hub_db = str(tmp_path / "hub_artifacts.db")
    monkeypatch.setenv("MARKET_DATA_ARTIFACTS_DB", hub_db)
    monkeypatch.delenv("PULSE_ARTIFACTS_DB", raising=False)
    monkeypatch.delenv("CRAWLER_ARTIFACTS_DB", raising=False)

    artifact_id = register_artifact(hub_db, repo="market-data-hub", kind="k", title="t", summary="s", content="c")

    record = get_everywhere("market-data-hub", artifact_id)
    assert record is not None
    assert record["title"] == "t"
    assert record["content"] == "c"


def test_get_everywhere_returns_none_for_unconfigured_repo(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_DATA_ARTIFACTS_DB", raising=False)
    monkeypatch.delenv("PULSE_ARTIFACTS_DB", raising=False)
    monkeypatch.delenv("CRAWLER_ARTIFACTS_DB", raising=False)
    assert get_everywhere("market-data-hub", "whatever") is None
