"""RegistryTools wraps db.status()/artifact_* as LazyBridge tools."""

from __future__ import annotations

import pytest

from lazytools.registry import RegistryTools

READ_ONLY_NAMES = {"registry_status", "artifact_search", "artifact_get"}
EXPECTED_NAMES = READ_ONLY_NAMES | {"artifact_register"}


def _tools(*, allow_write: bool = True):
    """Tests default to allow_write=True so existing artifact_register
    exercises keep working -- test_as_tools_is_read_only_by_default below
    covers the actual default."""
    provider = RegistryTools(allow_write=allow_write)
    by_name = {t.name: t for t in provider.as_tools()}
    return provider, by_name


def test_provider_is_tool_provider() -> None:
    assert RegistryTools()._is_lazy_tool_provider is True


def test_as_tools_is_read_only_by_default() -> None:
    """Matches every other MCP-exposed provider's convention
    (DataHubTools(allow_refresh=...), RegimeTools(allow_write=...), ...):
    a write tool (artifact_register) must not be emitted unless the caller
    explicitly opts in."""
    by_name = {t.name: t for t in RegistryTools().as_tools()}
    assert set(by_name) == READ_ONLY_NAMES
    assert "artifact_register" not in by_name


def test_as_tools_exposes_expected_names_when_write_enabled() -> None:
    _, by_name = _tools(allow_write=True)
    assert set(by_name) == EXPECTED_NAMES


def test_registry_status_delegates_to_db_status(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_DATA_DB", "/tmp/market.duckdb")
    _, by_name = _tools()
    out = by_name["registry_status"].run_sync()
    names = {row["name"] for row in out}
    assert "market_data" in names
    market_row = next(row for row in out if row["name"] == "market_data")
    assert market_row["set"] is True
    assert market_row["env_var"] == "MARKET_DATA_DB"


def test_artifact_register_writes_to_repo_artifact_db_and_search_finds_it(tmp_path, monkeypatch) -> None:
    hub_db = str(tmp_path / "hub_artifacts.db")
    monkeypatch.setenv("MARKET_DATA_ARTIFACTS_DB", hub_db)
    monkeypatch.delenv("PULSE_ARTIFACTS_DB", raising=False)
    monkeypatch.delenv("CRAWLER_ARTIFACTS_DB", raising=False)

    _, by_name = _tools()
    artifact_id = by_name["artifact_register"].run_sync(
        repo="market-data-hub",
        kind="backtest_report",
        title="SPY momentum backtest",
        summary="Sharpe 1.2 over 2020-2025",
        tags="equities,momentum",
        content="full payload here",
    )
    assert isinstance(artifact_id, str) and artifact_id

    results = by_name["artifact_search"].run_sync(query="momentum")
    assert len(results) == 1
    assert results[0]["title"] == "SPY momentum backtest"
    assert results[0]["repo"] == "market-data-hub"
    assert "content" not in results[0]

    record = by_name["artifact_get"].run_sync(repo="market-data-hub", artifact_id=artifact_id)
    assert record["content"] == "full payload here"
    assert record["tags"] == ["equities", "momentum"]


def test_artifact_register_unknown_repo_raises(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_DATA_ARTIFACTS_DB", raising=False)
    monkeypatch.delenv("PULSE_ARTIFACTS_DB", raising=False)
    monkeypatch.delenv("CRAWLER_ARTIFACTS_DB", raising=False)

    provider, _ = _tools()
    with pytest.raises(KeyError):
        provider._artifact_register(repo="not-a-real-repo", kind="k", title="t", summary="s")


def test_artifact_register_configured_repo_but_env_unset_raises_runtime_error(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_DATA_ARTIFACTS_DB", raising=False)
    provider, _ = _tools()
    with pytest.raises(RuntimeError, match="MARKET_DATA_ARTIFACTS_DB"):
        provider._artifact_register(repo="market-data-hub", kind="k", title="t", summary="s")


def test_artifact_get_delegates_to_get_everywhere(tmp_path, monkeypatch) -> None:
    pulse_db = str(tmp_path / "pulse_artifacts.db")
    monkeypatch.setenv("PULSE_ARTIFACTS_DB", pulse_db)
    monkeypatch.delenv("MARKET_DATA_ARTIFACTS_DB", raising=False)
    monkeypatch.delenv("CRAWLER_ARTIFACTS_DB", raising=False)

    _, by_name = _tools()
    artifact_id = by_name["artifact_register"].run_sync(
        repo="lazypulse", kind="k", title="t", summary="s",
    )
    assert by_name["artifact_get"].run_sync(repo="lazypulse", artifact_id=artifact_id) is not None
    assert by_name["artifact_get"].run_sync(repo="lazypulse", artifact_id="nope") is None
