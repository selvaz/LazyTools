"""Operations catalog persistence and deduplication tests."""

from __future__ import annotations

import json
from pathlib import Path

from lazytools.operations import OperationsCatalog
from lazytools.operations import integration as ops_integration
from lazytools.operations.portfolio import publish as publish_portfolio_run


def test_relative_paths_are_resolved_to_absolute(tmp_path: Path, monkeypatch) -> None:
    """A relative db_path/artifact_dir stays tied to the process's cwd at
    connection time -- a later os.chdir() would silently point the same
    instance at a different database. Resolve once at construction."""
    monkeypatch.chdir(tmp_path)
    catalog = OperationsCatalog("relative.sqlite", "relative-artifacts")
    assert catalog.db_path.is_absolute()
    assert catalog.artifact_dir.is_absolute()
    assert catalog.db_path == (tmp_path / "relative.sqlite").resolve()


def test_metadata_is_preserved_per_attachment_not_lost_to_content_dedup(tmp_path: Path) -> None:
    """Identical content registered by two different runs (e.g. an unchanged
    portfolio node published by both estimate and backtest) shares one
    physical artifact row, but each run's own metadata for that attachment
    must survive -- not just whichever run registered the content first."""
    catalog = OperationsCatalog(tmp_path / "operations.sqlite", tmp_path / "artifacts")
    estimate_run = catalog.start_run("portfolio_tree_estimate")
    backtest_run = catalog.start_run("portfolio_tree_backtest")

    a = catalog.register_json(estimate_run, "node.json", {"weight": 0.5}, kind="node_config",
                              metadata={"task": "portfolio_tree_estimate"})
    b = catalog.register_json(backtest_run, "node.json", {"weight": 0.5}, kind="node_config",
                              metadata={"task": "portfolio_tree_backtest"})

    assert a.artifact_id == b.artifact_id  # same content -> one physical artifact
    assert a.metadata == {"task": "portfolio_tree_estimate"}
    assert b.metadata == {"task": "portfolio_tree_backtest"}

    # artifacts_for_run() must report each run's own metadata too, not
    # whichever one happened to insert the shared artifacts row first.
    [from_estimate] = catalog.artifacts_for_run(estimate_run)
    [from_backtest] = catalog.artifacts_for_run(backtest_run)
    assert from_estimate.metadata == {"task": "portfolio_tree_estimate"}
    assert from_backtest.metadata == {"task": "portfolio_tree_backtest"}


def test_reregistering_within_the_same_run_updates_metadata(tmp_path: Path) -> None:
    """The *same* run can legitimately re-register identical content under
    the same name/kind/role more than once (e.g. a retry that enriches an
    attachment) -- the latest metadata must win, not the first, and
    artifacts_for_run() must agree with what register_json() just returned."""
    catalog = OperationsCatalog(tmp_path / "operations.sqlite", tmp_path / "artifacts")
    run_id = catalog.start_run("task-a")

    first = catalog.register_json(run_id, "node.json", {"weight": 0.5}, kind="node_config",
                                  metadata={"attempt": 1})
    second = catalog.register_json(run_id, "node.json", {"weight": 0.5}, kind="node_config",
                                   metadata={"attempt": 2})

    assert first.artifact_id == second.artifact_id
    assert second.metadata == {"attempt": 2}
    [attached] = catalog.artifacts_for_run(run_id)
    assert attached.metadata == {"attempt": 2}


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


def test_get_run_and_list_runs_expose_parent_run_id(tmp_path: Path) -> None:
    catalog = OperationsCatalog(tmp_path / "operations.sqlite", tmp_path / "artifacts")
    parent_id = catalog.start_run("parent-task")
    child_id = catalog.start_run("child-task", parent_run_id=parent_id)

    child = catalog.get_run(child_id)
    assert child is not None
    assert child.parent_run_id == parent_id

    [listed_child] = catalog.list_runs(task_name="child-task")
    assert listed_child.parent_run_id == parent_id

    parent = catalog.get_run(parent_id)
    assert parent is not None
    assert parent.parent_run_id is None


def _artifact_files(artifact_dir: Path) -> list[Path]:
    return [p for p in artifact_dir.rglob("*") if p.is_file()]


def test_identical_artifacts_are_deduplicated(tmp_path: Path) -> None:
    catalog = OperationsCatalog(tmp_path / "operations.sqlite", tmp_path / "artifacts")
    first = catalog.start_run("task-a")
    second = catalog.start_run("task-b")
    a = catalog.register_text(first, "same.txt", "same", kind="result")
    b = catalog.register_text(second, "same.txt", "same", kind="result")

    assert a.artifact_id == b.artifact_id
    assert len(list((tmp_path / "artifacts").rglob("*same*"))) == 0
    assert len(_artifact_files(tmp_path / "artifacts")) == 1


def test_identical_bytes_dedupe_across_different_extensions(tmp_path: Path) -> None:
    """Same content registered as "model.bin" then "checkpoint.pt" must land
    on one physical file, not two -- storage is keyed by digest alone, not
    digest+suffix, so a caller-chosen extension can't defeat content-address
    dedup."""
    catalog = OperationsCatalog(tmp_path / "operations.sqlite", tmp_path / "artifacts")
    run_id = catalog.start_run("task-a")
    a = catalog.register_bytes(run_id, "model.bin", b"weights", kind="model")
    b = catalog.register_bytes(run_id, "checkpoint.pt", b"weights", kind="model")

    assert a.artifact_id != b.artifact_id  # distinct rows (different name/kind identity)...
    assert a.storage_path == b.storage_path  # ...but exactly one physical file
    assert len(_artifact_files(tmp_path / "artifacts")) == 1


def test_null_role_artifacts_still_dedupe(tmp_path: Path) -> None:
    """SQLite treats NULL != NULL in a UNIQUE index, so a bare NULL role used
    to defeat INSERT OR IGNORE and create a duplicate run_artifacts row on
    every re-registration of the same content without an explicit role."""
    catalog = OperationsCatalog(tmp_path / "operations.sqlite", tmp_path / "artifacts")
    run_id = catalog.start_run("task-x")
    a = catalog.register_text(run_id, "same.txt", "same-content", kind="result")
    b = catalog.register_text(run_id, "same.txt", "same-content", kind="result")

    assert a.artifact_id == b.artifact_id
    assert a.role is None  # normalized "" is still reported as None to callers
    assert len(catalog.artifacts_for_run(run_id)) == 1


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
    # already stored under "result", just under a second name/kind. "config"
    # is the full resolved tree config (here, just the nodes list) kept
    # alongside it.
    assert {a.kind for a in artifacts} == {"result", "weights", "node_config", "config"}
    result_artifact = next(a for a in artifacts if a.kind == "result")
    with catalog._session() as con:
        node = con.execute("SELECT name, description FROM portfolio_nodes WHERE run_id=?", (run_id,)).fetchone()
        report = con.execute("SELECT title, artifact_id FROM reports WHERE run_id=?", (run_id,)).fetchone()
    assert tuple(node) == ("Free node", "Unrestricted sleeve")
    # Still shows up under `reports` (title = task name), pointing at the
    # same artifact_id as the "result" entry -- no bytes duplicated.
    assert tuple(report) == ("portfolio_tree_estimate", result_artifact.artifact_id)


def test_publish_marks_run_failed_when_a_later_step_raises(tmp_path: Path, monkeypatch) -> None:
    """If start_run() succeeds but a later catalog write raises, the run must
    end up "failed", not stuck "running" forever -- publish() itself still
    reports failure to its caller by returning None."""
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    def _boom(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(OperationsCatalog, "register_node", _boom)

    run_id = publish_portfolio_run(
        "portfolio_tree_estimate",
        parameters={"name": "demo"},
        result={"terminal_weights": {"ticker:SPY": 1.0}},
        config={"nodes": [{"id": "n1", "name": "N1"}]},
    )

    assert run_id is None
    catalog = OperationsCatalog()
    runs = catalog.list_runs(task_name="portfolio_tree_estimate")
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert "boom" in (runs[0].error or "")


def test_publish_persists_the_full_resolved_config(tmp_path: Path, monkeypatch) -> None:
    """`config` carries the tree actually used (saved-tree/data defaults +
    call-time overrides already merged) -- `parameters` alone can hold
    0/""/None placeholders whenever the caller relied on those defaults,
    and never carries the resolved `data`/`root_id` fields at all, so two
    runs over different date ranges could look identical in the catalog."""
    monkeypatch.setenv("LAZYTOOLS_OPERATIONS_DB", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("LAZYTOOLS_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    resolved_config = {
        "root_id": "root",
        "data": {"start": "2020-01-01", "end": "2026-01-01"},
        "backtest": {"train_size": 104, "rebalance_frequency": "M", "transaction_cost_bps": 5.0},
    }
    run_id = publish_portfolio_run(
        "portfolio_tree_backtest",
        parameters={"name": "demo", "train_size": 0, "rebalance_frequency": ""},
        result={"ok": True},
        config=resolved_config,
    )

    assert run_id is not None
    catalog = OperationsCatalog()
    config_artifact = next(a for a in catalog.artifacts_for_run(run_id) if a.kind == "config")
    stored = json.loads(Path(config_artifact.storage_path).read_text(encoding="utf-8"))
    assert stored == resolved_config


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
