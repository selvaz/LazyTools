"""Tool schema and behavior tests for the Node Copilot's read-only tool
profile over lazyportfolio.copilot (docs/node-copilot-operational-plan.md §7.2).

Mirrors test_fin_tree_tools.py's fake-backend pattern.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("lazyportfolio", reason="node copilot tools require lazyportfolio")

from lazyportfolio import OptimizationDataset
from lazyportfolio.copilot.repository import create_tree
from lazyportfolio.v2 import run_history

from lazytools.connectors.fin.node_copilot_tools import NodeCopilotReadTools


def _tree_config() -> dict[str, object]:
    return {
        "root_id": "root",
        "currency": "USD",
        "nodes": [
            {
                "id": "root",
                "name": "Root",
                "children": ["equity", "bond"],
                "instruments": [],
                "goal": {"objective": "min_risk"},
                "constraints": {},
            },
            {
                "id": "equity",
                "name": "Equity",
                "children": ["equity_us", "equity_intl"],
                "instruments": [],
                "proxy": "ticker:ACWI",
                "goal": {"objective": "max_ratio"},
                "constraints": {},
            },
            {
                "id": "equity_us",
                "name": "Equity US",
                "children": [],
                "instruments": ["ticker:VTI"],
                "proxy": "ticker:VTI",
                "goal": {"objective": "min_risk"},
                "constraints": {},
            },
            {
                "id": "equity_intl",
                "name": "Equity Intl",
                "children": [],
                "instruments": ["ticker:VXUS"],
                "proxy": "ticker:VXUS",
                "goal": {"objective": "min_risk"},
                "constraints": {},
            },
            {
                "id": "bond",
                "name": "Bond",
                "children": [],
                "instruments": ["ticker:AGG"],
                "proxy": "ticker:AGG",
                "goal": {"objective": "min_risk"},
                "constraints": {},
            },
        ],
        "backtest": {
            "benchmark": {
                "name": "B0",
                "weights": {"ticker:VTI": 0.4, "ticker:VXUS": 0.2, "ticker:AGG": 0.4},
            }
        },
    }


@pytest.fixture()
def tree_id(tmp_path) -> str:
    store_path = str(tmp_path / "store.sqlite3")
    revision = create_tree(
        _tree_config(), actor_type="human", actor_id="test", db_path=store_path
    )
    return revision.tree_id


@pytest.fixture()
def store_path(tmp_path) -> str:
    return str(tmp_path / "store.sqlite3")


class _FakeBackend:
    def __init__(self, frame) -> None:
        self.frame = frame

    def load_returns(self, instruments, *, start="", end="", frequency="D", currency=None):
        return OptimizationDataset(
            returns=self.frame.loc[:, instruments],
            metadata={"source": "fake-hub", "database_identity": "fake-hub"},
        )


@pytest.fixture()
def frame():
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(20260810)
    index = pd.bdate_range("2020-01-01", periods=300)
    returns = pd.DataFrame(
        {
            "ticker:VTI": rng.normal(0.0005, 0.01, len(index)),
            "ticker:VXUS": rng.normal(0.0003, 0.008, len(index)),
            "ticker:AGG": rng.normal(0.0002, 0.004, len(index)),
        },
        index=index,
    )
    returns["ticker:ACWI"] = 0.5 * returns["ticker:VTI"] + 0.5 * returns["ticker:VXUS"]
    return returns


def test_as_tools_exposes_exactly_the_seven_read_only_tools(tree_id, store_path) -> None:
    tools = {t.name for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    assert tools == {
        "tree_get_node_context",
        "tree_get_parent_context",
        "tree_get_child_summaries",
        "tree_get_revision",
        "tree_get_recent_runs",
        "portfolio_tree_validate_views",
        "portfolio_tree_estimate_counterfactual",
    }


def test_get_node_context_for_an_interior_node(tree_id, store_path) -> None:
    tools = {t.name: t for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    payload = json.loads(
        tools["tree_get_node_context"].run_sync(tree_id=tree_id, node_id="equity")
    )
    assert payload["ok"] is True
    context = payload["context"]
    assert context["node_id"] == "equity"
    assert set(context["allowed_view_instruments"]) == {"ticker:VTI", "ticker:VXUS"}
    assert context["child_node_ids"] == ["equity_us", "equity_intl"]
    assert context["parent_node_id"] == "root"


def test_get_node_context_for_unknown_tree_raises(store_path) -> None:
    tools = {t.name: t for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    with pytest.raises(Exception, match="no revisions yet"):
        tools["tree_get_node_context"].run_sync(tree_id="does-not-exist", node_id="equity")


def test_get_parent_context_resolves_the_immediate_parent(tree_id, store_path) -> None:
    tools = {t.name: t for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    payload = json.loads(
        tools["tree_get_parent_context"].run_sync(tree_id=tree_id, node_id="equity_us")
    )
    assert payload["ok"] is True
    assert payload["context"]["node_id"] == "equity"


def test_get_parent_context_for_the_root_returns_ok_false_not_an_error(
    tree_id, store_path
) -> None:
    tools = {t.name: t for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    payload = json.loads(
        tools["tree_get_parent_context"].run_sync(tree_id=tree_id, node_id="root")
    )
    assert payload["ok"] is False
    assert "error" in payload


def test_get_child_summaries_lists_direct_children_only(tree_id, store_path) -> None:
    tools = {t.name: t for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    payload = json.loads(
        tools["tree_get_child_summaries"].run_sync(tree_id=tree_id, node_id="equity")
    )
    assert payload["ok"] is True
    assert {c["id"] for c in payload["children"]} == {"equity_us", "equity_intl"}
    assert all("proxy" in c and "objective" in c for c in payload["children"])


def test_get_revision_returns_head_metadata(tree_id, store_path) -> None:
    tools = {t.name: t for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    payload = json.loads(tools["tree_get_revision"].run_sync(tree_id=tree_id))
    assert payload["ok"] is True
    assert payload["revision_id"]
    assert payload["parent_revision_id"] is None
    assert payload["actor_type"] == "human"
    assert payload["actor_id"] == "test"


def test_get_recent_runs_reflects_recorded_history(tree_id, store_path) -> None:
    run_history.record_run(
        cache_key="test-cache-key",
        path="/api/v2/estimate",
        kind="estimate",
        tree_id="My Tree",
        config_hash="abc",
        data_as_of="2026-08-01",
        data_fingerprint="fp",
        weights={"ticker:VTI": 1.0},
        metrics=None,
        payload={"ok": True},
        db_path=store_path,
    )
    tools = {t.name: t for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    payload = json.loads(tools["tree_get_recent_runs"].run_sync(name="My Tree"))
    assert payload["ok"] is True
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["kind"] == "estimate"


def test_validate_views_accepts_a_view_inside_the_universe(tree_id, store_path) -> None:
    tools = {t.name: t for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    payload = json.loads(
        tools["portfolio_tree_validate_views"].run_sync(
            tree_id=tree_id,
            node_id="equity",
            views=[
                {
                    "instruments": {"ticker:VTI": 1.0, "ticker:VXUS": -1.0},
                    "expected_return": 0.02,
                    "confidence": 0.6,
                    "rationale": "test",
                }
            ],
        )
    )
    assert payload["ok"] is True
    assert payload["validation"]["valid"] is True


def test_validate_views_rejects_an_instrument_outside_the_universe(tree_id, store_path) -> None:
    tools = {t.name: t for t in NodeCopilotReadTools(store_path=store_path).as_tools()}
    payload = json.loads(
        tools["portfolio_tree_validate_views"].run_sync(
            tree_id=tree_id,
            node_id="equity",
            views=[
                {
                    "instruments": {"ticker:AGG": 1.0},
                    "expected_return": 0.02,
                    "confidence": 0.6,
                    "rationale": "test",
                }
            ],
        )
    )
    assert payload["ok"] is True  # the call succeeded; the view set failed validation
    assert payload["validation"]["valid"] is False
    assert any(
        e["code"] == "instrument_outside_universe" for e in payload["validation"]["errors"]
    )


def test_estimate_counterfactual_returns_a_diff_never_raw_returns(
    tree_id, store_path, frame
) -> None:
    backend = _FakeBackend(frame)
    tools = {
        t.name: t
        for t in NodeCopilotReadTools(backend=backend, store_path=store_path).as_tools()
    }
    payload = json.loads(
        tools["portfolio_tree_estimate_counterfactual"].run_sync(
            tree_id=tree_id,
            node_id="equity",
            views=[
                {
                    "instruments": {"ticker:VTI": 1.0, "ticker:VXUS": -1.0},
                    "expected_return": 0.03,
                    "confidence": 0.6,
                    "rationale": "test",
                }
            ],
        )
    )
    assert payload["ok"] is True
    counterfactual = payload["counterfactual"]
    assert "terminal_weights" in counterfactual["delta"]
    assert "returns" not in counterfactual["baseline"]
    assert "returns" not in counterfactual["variant"]
