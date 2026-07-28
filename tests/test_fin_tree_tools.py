"""Tool schema and behavior tests for the LazyPortfolio hierarchical tree surface.

Mirrors ``test_portfolio_optimization_tools.py``'s fake-backend pattern for its
sibling flat-node connector.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("lazyfin", reason="fin connector requires lazyfin")
pytest.importorskip("lazyportfolio", reason="portfolio optimization requires lazyportfolio")

from lazyportfolio import OptimizationDataset

from lazytools.connectors.fin.tree_tools import PortfolioTreeTools


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
