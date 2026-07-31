"""Reusable best-effort bridge from domain repos to the operations catalog.

market-data-hub and LazyCrawler used to each carry their own copy of this
start/finish/register_* wiring. Keeping one implementation here means a fix
only has to happen once. Each domain repo still needs its own thin,
import-guarded shim (LazyTools may not be installed there) -- see
``docs/operations.md`` for the pattern.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from lazytools.operations.catalog import OperationsCatalog


def is_disabled() -> bool:
    """True when ``LAZYTOOLS_OPERATIONS_DISABLED`` opts out of catalog writes.

    Scheduled jobs are expected to publish by default. Interactive/exploratory
    callers (e.g. an agent driving the MCP server) can set this once to keep
    ad-hoc or test runs out of the shared catalog and artifact store, which
    has no retention/pruning policy.
    """
    return os.environ.get("LAZYTOOLS_OPERATIONS_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def start(task_name: str, *, source_repo: str, parameters: dict[str, Any],
          source_db: str | None = None) -> tuple[OperationsCatalog | None, str | None]:
    if is_disabled():
        return None, None
    try:
        catalog = OperationsCatalog()
        run_id = catalog.start_run(task_name, parameters=parameters, source_repo=source_repo, source_db=source_db)
    except Exception as exc:
        print(f"Operations catalog unavailable; continuing without registration: {exc}", file=sys.stderr)
        return None, None
    # stderr, not stdout: this also runs inside the MCP stdio request handler
    # (lazytools-mcp --allow-unsafe), which reserves stdout for JSON-RPC --
    # a stray stdout line there would corrupt the protocol stream.
    print(f"OPERATIONS_RUN_ID={run_id}", file=sys.stderr)
    return catalog, run_id


def finish(catalog: Any, run_id: str | None, *, ok: bool, error: str | None = None) -> None:
    if catalog is None or run_id is None:
        return
    try:
        catalog.finish_run(run_id, "succeeded" if ok else "failed", error=error)
    except Exception as exc:
        print(f"Operations catalog update failed: {exc}", file=sys.stderr)


def register_file(catalog: Any, run_id: str | None, path: str | Path, *, kind: str = "artifact",
                  role: str | None = None) -> None:
    if catalog is None or run_id is None or not Path(path).is_file():
        return
    try:
        catalog.register_file(run_id, path, kind=kind, role=role)
    except Exception as exc:
        print(f"Operations artifact registration failed for {path}: {exc}", file=sys.stderr)


def register_json(catalog: Any, run_id: str | None, name: str, value: Any, *, kind: str = "result") -> None:
    if catalog is None or run_id is None:
        return
    try:
        catalog.register_json(run_id, name, value, kind=kind, role=kind)
    except Exception as exc:
        print(f"Operations JSON registration failed for {name}: {exc}", file=sys.stderr)
