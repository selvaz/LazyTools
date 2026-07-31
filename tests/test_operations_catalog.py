"""Operations catalog persistence and deduplication tests."""

from __future__ import annotations

from pathlib import Path

from lazytools.operations import OperationsCatalog
from lazytools.operations.portfolio import publish as publish_portfolio_run


def test_run_and_artifacts_round_trip(tmp_path: Path) -> None:
    catalog = OperationsCatalog(tmp_path / "operations.sqlite", tmp_path / "artifacts")
    run_id = catalog.start_run("crawler_3x_daily", parameters={"preset": "news_scan"}, source_repo="LazyCrawler")
    report = catalog.register_report(run_id, "Crawler report", "# Report\n", name="report.md")
    result = catalog.register_json(run_id, "result.json", {"items": 3}, kind="result")
    catalog.finish_run(run_id)

    run = catalog.get_run(run_id)
    assert run is not None
    assert run.status == "succeeded"
    assert run.parameters == {"preset": "news_scan"}
    assert Path(report.storage_path).read_text(encoding="utf-8") == "# Report\n"
    assert result.kind == "result"
    assert {a.name for a in catalog.artifacts_for_run(run_id)} == {"report.md", "result.json"}


def test_identical_artifacts_are_deduplicated(tmp_path: Path) -> None:
    catalog = OperationsCatalog(tmp_path / "operations.sqlite", tmp_path / "artifacts")
    first = catalog.start_run("task-a")
    second = catalog.start_run("task-b")
    a = catalog.register_text(first, "same.txt", "same", kind="result")
    b = catalog.register_text(second, "same.txt", "same", kind="result")

    assert a.artifact_id == b.artifact_id
    assert len(list((tmp_path / "artifacts").rglob("*same*"))) == 0
    assert len(list((tmp_path / "artifacts").rglob("*.txt"))) == 1


def test_failed_run_is_recorded(tmp_path: Path) -> None:
    catalog = OperationsCatalog(tmp_path / "operations.sqlite", tmp_path / "artifacts")
    run_id = catalog.start_run("failing-task")
    catalog.fail_run(run_id, "network timeout")
    run = catalog.get_run(run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.error == "network timeout"


def test_portfolio_outputs_store_weights_and_described_nodes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    run_id = publish_portfolio_run(
        "portfolio_tree_estimate",
        parameters={"name": "free-node-demo"},
        result={"terminal_weights": {"ticker:SPY": 0.6, "ticker:TLT": 0.4}},
        config={"nodes": [{"id": "free_1", "name": "Free node", "description": "Unrestricted sleeve"}]},
    )
    assert run_id is not None
    catalog = OperationsCatalog()
    artifacts = catalog.artifacts_for_run(run_id)
    assert {a.kind for a in artifacts} == {"result", "weights", "node_config"}
    with catalog._connect() as con:
        node = con.execute("SELECT name, description FROM portfolio_nodes WHERE run_id=?", (run_id,)).fetchone()
    assert tuple(node) == ("Free node", "Unrestricted sleeve")
