"""Command line installer and inspection commands for the operations catalog."""

from __future__ import annotations

import argparse

from lazytools.operations.catalog import OperationsCatalog


def main() -> int:
    parser = argparse.ArgumentParser(prog="lazytools-operations")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="initialize the catalog and artifact store")
    init.add_argument("--db")
    init.add_argument("--artifacts")
    ls = sub.add_parser("list-runs", help="list recent task runs")
    ls.add_argument("--db")
    ls.add_argument("--task")
    ls.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    catalog = OperationsCatalog(getattr(args, "db", None), getattr(args, "artifacts", None))
    if args.command == "init":
        catalog.initialize()
        print(f"operations database: {catalog.db_path.resolve()}")
        print(f"artifact store: {catalog.artifact_dir.resolve()}")
        return 0
    for run in catalog.list_runs(task_name=args.task, limit=args.limit):
        print(f"{run.run_id}\t{run.status}\t{run.task_name}\t{run.started_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
