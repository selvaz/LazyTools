"""Tool schema and behavior tests for the LazyPortfolio hierarchical tree surface.

Mirrors ``test_portfolio_optimization_tools.py``'s fake-backend pattern for its
sibling flat-node connector.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("lazyfin", reason="fin connector requires lazyfin")
pytest.importorskip("lazyportfolio", reason="portfolio optimization requires lazyportfolio")

from lazyportfolio import OptimizationDataset

from lazytools.connectors.fin.tree_tools import PortfolioTreeTools


@pytest.fixture(autouse=True)
def _isolated_operations_catalog(tmp_path, monkeypatch):
    """Every portfolio_tree_estimate/backtest call in this file publishes to
    the operations catalog as a side effect (unconditionally, since round 2
    of the operations-catalog work) -- autouse so no test here, present or
    future, can slip through and write into the developer's real
    ~/.lazytools/operations.sqlite the way the individually-patched wiring
    tests alone did not catch.
    """
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "auto-ops.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "auto-ops-artifacts"))


def _tree_config(**backtest_overrides: object) -> dict[str, object]:
    return {
        "root_id": "root",
        "nodes": [
            {
                "id": "root",
                "name": "Root",
                "children": ["equity"],
                "instruments": ["AGG"],
                "proxy": "",
                "goal": {"objective": "min_risk"},
                "constraints": {},
            },
            {
                "id": "equity",
                "name": "Equity",
                "children": [],
                "instruments": ["SPY", "TLT"],
                "proxy": "ACWI",
                "goal": {"objective": "min_risk"},
                "constraints": {},
            },
        ],
        "data": {"start": "", "end": ""},
        "backtest": {
            "id": "test",
            "train_size": 20,
            "rebalance_frequency": "M",
            "estimation_frequency": "W",
            "transaction_cost_bps": 0,
            "forward_enabled": True,
            "hierarchy_mode": "proxy",
            "benchmark": {"name": "B0", "weights": {"ACWI": 0.7, "AGG": 0.3}},
            **backtest_overrides,
        },
    }


class _FakeTreeBackend:
    def __init__(self, frame) -> None:
        self.frame = frame
        self.calls: list[dict[str, object]] = []

    def load_returns(self, instruments, *, start="", end="", frequency="D"):
        self.calls.append({"instruments": instruments, "start": start, "end": end, "frequency": frequency})
        return OptimizationDataset(
            returns=self.frame.loc[:, instruments],
            metadata={"source": "fake-hub", "n_rows": len(self.frame), "raw_rows": "never expose"},
        )


@pytest.fixture()
def frame():
    pd = pytest.importorskip("pandas")
    return pd.DataFrame(
        {
            "ticker:AGG": [0.0003 * ((index % 5) - 2) for index in range(400)],
            "ticker:SPY": [0.001 * ((index % 7) - 3) for index in range(400)],
            "ticker:TLT": [0.0005 * ((index % 5) - 2) for index in range(400)],
            "ticker:ACWI": [0.0008 * ((index % 6) - 2) for index in range(400)],
        },
        index=pd.date_range("2024-01-02", periods=400, freq="B"),
    )


# --------------------------------------------------------------------------- #
# Tool-name contract
# --------------------------------------------------------------------------- #

READ_TOOLS = {"portfolio_tree_validate", "portfolio_tree_list", "portfolio_tree_load"}
WRITE_TOOLS = {
    "portfolio_tree_save",
    "portfolio_tree_delete",
    "portfolio_tree_estimate",
    "portfolio_tree_backtest",
}


def test_read_only_provider_exposes_only_reads() -> None:
    assert {t.name for t in PortfolioTreeTools().as_tools()} == READ_TOOLS


def test_write_enabled_provider_exposes_everything() -> None:
    assert {t.name for t in PortfolioTreeTools(allow_write=True).as_tools()} == READ_TOOLS | WRITE_TOOLS


# --------------------------------------------------------------------------- #
# Validate
# --------------------------------------------------------------------------- #


def test_validate_reports_ok_and_sleeves_for_a_valid_tree() -> None:
    tools = {t.name: t for t in PortfolioTreeTools().as_tools()}
    payload = json.loads(tools["portfolio_tree_validate"].run_sync(config=_tree_config()))
    assert payload["ok"] is True
    assert payload["root_has_children"] is True
    assert {s["node"] for s in payload["sleeves"]} == {"Equity"}
    assert set(payload["instruments"]) == {"ticker:AGG", "ticker:SPY", "ticker:TLT", "ticker:ACWI"}


def test_validate_reports_a_clear_error_for_an_invalid_tree_without_raising() -> None:
    config = _tree_config()
    config["nodes"][0]["children"] = ["missing-child"]
    tools = {t.name: t for t in PortfolioTreeTools().as_tools()}
    payload = json.loads(tools["portfolio_tree_validate"].run_sync(config=config))
    assert payload["ok"] is False
    assert "unknown child id" in payload["error"]


# --------------------------------------------------------------------------- #
# Save / list / load / delete round-trip
# --------------------------------------------------------------------------- #


def test_save_list_load_delete_round_trip(tmp_path) -> None:
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, store_dir=str(tmp_path)).as_tools()}
    config = _tree_config()

    saved = json.loads(tools["portfolio_tree_save"].run_sync(name="My Tree", config=config))
    assert saved["ok"] is True
    assert saved["name"] == "My Tree"

    listed = json.loads(tools["portfolio_tree_list"].run_sync())
    assert [item["name"] for item in listed["items"]] == ["My Tree"]
    assert listed["directory"] == saved["directory"]

    loaded = json.loads(tools["portfolio_tree_load"].run_sync(name="My Tree"))
    assert loaded == config

    deleted = json.loads(tools["portfolio_tree_delete"].run_sync(name="My Tree"))
    assert deleted["ok"] is True
    assert json.loads(tools["portfolio_tree_list"].run_sync())["items"] == []


def test_save_rejects_an_invalid_tree_and_writes_nothing(tmp_path) -> None:
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, store_dir=str(tmp_path)).as_tools()}
    config = _tree_config()
    config["nodes"][1]["proxy"] = ""  # a child with no proxy fails validation

    with pytest.raises(Exception, match="proxy is required"):
        tools["portfolio_tree_save"].run_sync(name="bad", config=config)
    assert list(tmp_path.glob("*.json")) == []


# --------------------------------------------------------------------------- #
# Estimate / backtest
# --------------------------------------------------------------------------- #


def test_estimate_runs_forward_mode_and_never_leaks_synthetic_returns(frame) -> None:
    backend = _FakeTreeBackend(frame)
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, backend=backend).as_tools()}

    payload = json.loads(tools["portfolio_tree_estimate"].run_sync(config=_tree_config()))
    assert payload["ok"] is True
    assert payload["mode"] == "forward"
    assert set(payload["nodes"]) == {"Root", "Equity"}
    assert abs(sum(payload["terminal_weights"].values()) - 1.0) < 1e-6
    dumped = json.dumps(payload)
    assert "synthetic_returns" not in dumped
    assert "raw_rows" not in dumped


def test_estimate_registers_the_resolved_config_exactly_once_on_success(frame) -> None:
    """Round-8 added an early config snapshot to cover the failure path; it
    must not also get registered again by publish() on success, or the same
    tree config ends up attached to the run twice."""
    backend = _FakeTreeBackend(frame)
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, backend=backend).as_tools()}

    tools["portfolio_tree_estimate"].run_sync(config=_tree_config())

    from lazytools.operations import OperationsCatalog
    catalog = OperationsCatalog()
    [run] = catalog.list_runs(task_name="portfolio_tree_estimate")
    config_artifacts = [a for a in catalog.artifacts_for_run(run.run_id) if a.kind == "config"]
    assert len(config_artifacts) == 1


def test_estimate_config_wins_over_name_when_both_given(tmp_path, frame) -> None:
    backend = _FakeTreeBackend(frame)
    tools = {
        t.name: t for t in PortfolioTreeTools(allow_write=True, backend=backend, store_dir=str(tmp_path)).as_tools()
    }
    saved_config = _tree_config(hierarchy_mode="proxy")
    tools["portfolio_tree_save"].run_sync(name="saved", config=saved_config)

    inline_config = _tree_config(forward_enabled=False)  # -> flat, distinct from the saved tree's forward
    payload = json.loads(
        tools["portfolio_tree_estimate"].run_sync(config=inline_config, name="saved")
    )
    assert payload["mode"] == "flat"


def test_estimate_requires_config_or_name() -> None:
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True).as_tools()}
    with pytest.raises(Exception, match="either config or name must be given"):
        tools["portfolio_tree_estimate"].run_sync()


def test_backtest_runs_and_never_leaks_curves_or_return_rows(frame) -> None:
    backend = _FakeTreeBackend(frame)
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, backend=backend).as_tools()}

    payload = json.loads(tools["portfolio_tree_backtest"].run_sync(config=_tree_config()))
    assert payload["ok"] is True
    assert payload["n_folds"] > 0
    assert "cagr" in payload["metrics"]
    assert "curves" not in payload
    dumped = json.dumps(payload)
    assert "raw_rows" not in dumped


def test_backtest_registers_the_resolved_config_exactly_once_on_success(frame) -> None:
    """Round-8 added an early config snapshot to cover the failure path; it
    must not also get registered again by publish() on success, or the same
    tree config ends up attached to the run twice."""
    backend = _FakeTreeBackend(frame)
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, backend=backend).as_tools()}

    tools["portfolio_tree_backtest"].run_sync(config=_tree_config())

    from lazytools.operations import OperationsCatalog
    catalog = OperationsCatalog()
    [run] = catalog.list_runs(task_name="portfolio_tree_backtest")
    config_artifacts = [a for a in catalog.artifacts_for_run(run.run_id) if a.kind == "config"]
    assert len(config_artifacts) == 1


def test_backtest_override_changes_the_run_without_mutating_the_saved_file(tmp_path, frame) -> None:
    backend = _FakeTreeBackend(frame)
    tools = {
        t.name: t for t in PortfolioTreeTools(allow_write=True, backend=backend, store_dir=str(tmp_path)).as_tools()
    }
    config = _tree_config(train_size=20, rebalance_frequency="M")
    tools["portfolio_tree_save"].run_sync(name="saved", config=config)

    default_run = json.loads(tools["portfolio_tree_backtest"].run_sync(name="saved"))
    overridden_run = json.loads(
        tools["portfolio_tree_backtest"].run_sync(name="saved", rebalance_frequency="Q")
    )
    assert default_run["provenance"]["rebalance_frequency"] == "M"
    assert overridden_run["provenance"]["rebalance_frequency"] == "Q"

    reloaded = json.loads(tools["portfolio_tree_load"].run_sync(name="saved"))
    assert reloaded["backtest"]["rebalance_frequency"] == "M"


def test_estimate_publishes_to_operations_catalog(frame, tmp_path, monkeypatch) -> None:
    """Covers the actual wiring in tree_tools.py, not just publish() in isolation."""
    # Mocking publish() alone still leaves the real integration.start() call
    # active, which would otherwise write a permanently "running" record
    # into the developer's actual ~/.lazytools/operations.sqlite.
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    backend = _FakeTreeBackend(frame)
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, backend=backend).as_tools()}

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "lazytools.operations.portfolio.publish",
        lambda task_name, **kwargs: calls.append({"task_name": task_name, **kwargs}),
    )

    tools["portfolio_tree_estimate"].run_sync(config=_tree_config())

    assert len(calls) == 1
    assert calls[0]["task_name"] == "portfolio_tree_estimate"
    assert calls[0]["result"]["ok"] is True
    assert calls[0]["config"]["root_id"] == "root"


def test_backtest_publishes_to_operations_catalog(frame, tmp_path, monkeypatch) -> None:
    # Mocking publish() alone still leaves the real integration.start() call
    # active, which would otherwise write a permanently "running" record
    # into the developer's actual ~/.lazytools/operations.sqlite.
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    backend = _FakeTreeBackend(frame)
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, backend=backend).as_tools()}

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "lazytools.operations.portfolio.publish",
        lambda task_name, **kwargs: calls.append({"task_name": task_name, **kwargs}),
    )

    tools["portfolio_tree_backtest"].run_sync(config=_tree_config())

    assert len(calls) == 1
    assert calls[0]["task_name"] == "portfolio_tree_backtest"
    assert calls[0]["result"]["ok"] is True


def test_estimate_records_a_failed_catalog_run_when_load_returns_raises(tmp_path, monkeypatch) -> None:
    """The run must be registered *before* load_returns/estimation, so a
    failure there still shows up in the catalog as "failed" instead of no
    record existing at all."""
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    class _BrokenBackend:
        def load_returns(self, *args, **kwargs):
            raise RuntimeError("hub unavailable")

    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, backend=_BrokenBackend()).as_tools()}

    with pytest.raises(Exception, match="hub unavailable"):
        tools["portfolio_tree_estimate"].run_sync(config=_tree_config())

    from lazytools.operations import OperationsCatalog
    runs = OperationsCatalog().list_runs(task_name="portfolio_tree_estimate")
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert "hub unavailable" in (runs[0].error or "")


def test_estimate_records_a_failed_catalog_run_when_config_is_invalid(tmp_path, monkeypatch) -> None:
    """The run must be registered *before* resolving/parsing the tree config
    too -- an invalid inline/saved tree is exactly the kind of scheduled
    failure the catalog exists to surface."""
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    config = _tree_config()
    config["nodes"][0]["children"] = ["missing-child"]
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True).as_tools()}

    with pytest.raises(Exception, match="unknown child id"):
        tools["portfolio_tree_estimate"].run_sync(config=config)

    from lazytools.operations import OperationsCatalog
    runs = OperationsCatalog().list_runs(task_name="portfolio_tree_estimate")
    assert len(runs) == 1
    assert runs[0].status == "failed"
    # The supplied config must survive even though _resolve_config() never
    # got to `merged` -- otherwise a failed run can't say what caused it.
    assert runs[0].parameters["config"] == config


def test_estimate_records_resolved_named_config_before_parsing_fails(tmp_path, monkeypatch) -> None:
    """When called with `name=` instead of an inline config, parameters
    only has the (mutable, editable-later) name -- the resolved tree must
    still be captured right after it loads from disk, before from_config()
    can reject it, or a failure here is unrecoverable once the saved file
    changes."""
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "ops.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "ops-artifacts"))
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    tools = {t.name: t for t in PortfolioTreeTools(allow_write=True, store_dir=str(store_dir)).as_tools()}
    tools["portfolio_tree_save"].run_sync(name="saved", config=_tree_config())

    # Corrupt the saved file directly, bypassing portfolio_tree_save's own
    # validation, so _resolve_config() loads it fine but from_config()
    # rejects it later.
    [saved_file] = list(store_dir.glob("*.json"))
    broken_config = _tree_config()
    broken_config["nodes"][0]["children"] = ["missing-child"]
    saved_file.write_text(json.dumps(broken_config), encoding="utf-8")

    with pytest.raises(Exception, match="unknown child id"):
        tools["portfolio_tree_estimate"].run_sync(name="saved")

    from lazytools.operations import OperationsCatalog
    catalog = OperationsCatalog()
    runs = catalog.list_runs(task_name="portfolio_tree_estimate")
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].parameters["config"] is None  # loaded by name, not inline

    config_artifact = next(a for a in catalog.artifacts_for_run(runs[0].run_id) if a.kind == "config")
    stored = json.loads(Path(config_artifact.storage_path).read_text(encoding="utf-8"))
    assert stored == broken_config
