"""Best-effort publication of portfolio optimizer outputs."""

from __future__ import annotations

import sys
from typing import Any

from lazytools.operations.catalog import OperationsCatalog
from lazytools.operations.integration import is_disabled


def publish(task_name: str, *, parameters: dict[str, Any], result: dict[str, Any],
            config: dict[str, Any] | None = None) -> str | None:
    """Publish an optimizer result, weights and constructed nodes if enabled."""
    if is_disabled():
        return None
    catalog: OperationsCatalog | None = None
    run_id: str | None = None
    try:
        catalog = OperationsCatalog()
        run_id = catalog.start_run(task_name, parameters=parameters, source_repo="LazyTools")
        result_artifact = catalog.register_json(run_id, "optimizer-result.json", result,
                                                 kind="result", role="optimizer-result")
        catalog.link_report(run_id, task_name, result_artifact)
        weights = result.get("target_weights") or result.get("terminal_weights")
        if weights is not None:
            catalog.register_json(run_id, "portfolio-weights.json", weights, kind="weights", role="weights")
        backtest_settings = (config or {}).get("backtest")
        if isinstance(backtest_settings, dict) and backtest_settings:
            # config["backtest"] carries the *resolved* settings (saved-tree
            # defaults + call-time overrides already merged) -- persisting
            # `parameters` alone would record 0/""/None placeholders whenever
            # the caller relied on config/saved-tree defaults instead of
            # passing explicit overrides.
            catalog.register_json(run_id, "resolved-backtest-settings.json", backtest_settings,
                                  kind="config", role="backtest-settings")
        for node in (config or {}).get("nodes", []):
            if not isinstance(node, dict):
                continue
            node_key = str(node.get("id") or node.get("name") or "node")
            node_name = str(node.get("name") or node_key)
            catalog.register_node(
                run_id,
                node_key,
                name=node_name,
                description=(node.get("description") or node.get("summary")
                             or f"Constructed portfolio node: {node_name}"),
                node_type=str(node.get("type") or "portfolio"),
                config=node,
                metadata={"task": task_name},
            )
        catalog.finish_run(run_id, "succeeded")
        return run_id
    except Exception as exc:
        print(f"Optimizer result was not published to operations catalog: {exc}", file=sys.stderr)
        if catalog is not None and run_id is not None:
            try:
                catalog.fail_run(run_id, str(exc))
            except Exception:
                pass  # best-effort: the run just stays "running" if even this fails
        return None
