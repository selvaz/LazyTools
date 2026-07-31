"""Operations catalog persistence and deduplication tests."""

from __future__ import annotations

from pathlib import Path

from lazytools.operations import OperationsCatalog
from lazytools.operations import integration as ops_integration
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
    # No separate "report" artifact: it used to duplicate the exact same JSON
    # already stored under "result", just under a second name/kind.
    assert {a.kind for a in artifacts} == {"result", "weights", "node_config"}
    result_artifact = next(a for a in artifacts if a.kind == "result")
    with catalog._session() as con:
        node = con.execute("SELECT name, description FROM portfolio_nodes WHERE run_id=?", (run_id,)).fetchone()
        report = con.execute("SELECT title, artifact_id FROM reports WHERE run_id=?", (run_id,)).fetchone()
    assert tuple(node) == ("Free node", "Unrestricted sleeve")
    # Still shows up under `reports` (title = task name), pointing at the
    # same artifact_id as the "result" entry -- no bytes duplicated.
    assert tuple(report) == ("portfolio_tree_estimate", result_artifact.artifact_id)


def test_publish_portfolio_run_is_skipped_when_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DISABLED", "1")

    run_id = publish_portfolio_run(
        "portfolio_tree_estimate",
        parameters={"name": "free-node-demo"},
        result={"terminal_weights": {"ticker:SPY": 1.0}},
    )

    assert run_id is None
    assert not (tmp_path / "operations.sqlite").exists()


def test_integration_start_is_skipped_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DISABLED", "true")
    catalog, run_id = ops_integration.start("crawler_news_crawl", source_repo="LazyCrawler", parameters={})
    assert (catalog, run_id) == (None, None)


def test_integration_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LAZYTOOLS_OPERATIONS_DISABLED", raising=False)
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    catalog, run_id = ops_integration.start(
        "crawler_news_crawl", source_repo="LazyCrawler", parameters={"preset": "news_scan"},
    )
    assert catalog is not None and run_id is not None

    report_path = tmp_path / "digest.md"
    report_path.write_text("# Digest\n", encoding="utf-8")
    ops_integration.register_file(catalog, run_id, report_path, kind="report", role="digest")
    ops_integration.register_json(catalog, run_id, "summary.json", {"items": 2})
    ops_integration.finish(catalog, run_id, ok=True)

    run = catalog.get_run(run_id)
    assert run is not None
    assert run.status == "succeeded"
    assert run.source_repo == "LazyCrawler"
    assert {a.name for a in catalog.artifacts_for_run(run_id)} == {"digest.md", "summary.json"}
