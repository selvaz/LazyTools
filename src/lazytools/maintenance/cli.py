"""Standalone CLI for read-only code maintenance, with no MCP or LazyCEO.

Configuration never grants executable test commands or source writes.
Approved checks and reviewer Agents are separate Python API capabilities.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from lazytools.codemap import RepoMapError

from .config import load_config
from .core import MaintenanceError, assert_repo_root, git_status, resolve_sha
from .pipeline import MaintenancePipeline


def _base_arg(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Use --base REPO=GIT_REF")
    name, ref = value.split("=", 1)
    if not name or not ref or len(ref) > 200:
        raise argparse.ArgumentTypeError("Use --base REPO=GIT_REF")
    return name, ref


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lazytools-maintenance",
        description="Produce commit-pinned, read-only RepoMap maintenance evidence.",
    )
    parser.add_argument("--config", required=True, help="Operator-owned workspace TOML")
    parser.add_argument(
        "--base", action="append", type=_base_arg, default=[], metavar="REPO=REF",
        help="Compare configured repository HEAD to a base ref (repeatable)",
    )
    parser.add_argument(
        "--executable", default="repomap", help="Explicit local repomap executable path",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Resolve permitted roots and HEADs without running repomap",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print bounded JSON summary to stdout",
    )
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        base: dict[str, str] = {}
        for repo_id, ref in args.base:
            if repo_id in base:
                raise ValueError(f"Duplicate --base for repository {repo_id}")
            base[repo_id] = ref
        unknown = set(base) - set(cfg.workspace.paths())
        if unknown:
            raise ValueError(f"Unknown repositories in --base: {', '.join(sorted(unknown))}")
        if args.validate_only:
            shas: dict[str, str] = {}
            for repo in cfg.workspace.repos:
                assert_repo_root(repo.root)
                if cfg.workspace.require_clean and git_status(repo.root) != "clean":
                    raise MaintenanceError(f"{repo.id}: dirty Git checkout")
                shas[repo.id] = resolve_sha(repo.root)
            result: dict[str, object] = {"status": "valid", "sha": shas}
        else:
            report = MaintenancePipeline(
                cfg.workspace, executable=args.executable,
                use_embeddings=cfg.embeddings, timeout_seconds=cfg.timeout_seconds,
            ).execute(base_refs=base)
            result = {
                "status": report["status"], "verified": report["verified"],
                "sha": report["sha"], "verification": report["verification"],
                "report_json": report["report_json"],
                "report_markdown": report["report_markdown"],
                "warnings": report["warnings"],
                "cross_repo_candidates": report["cross_repo_candidates"],
            }
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"Status: {result['status']}")
            for name, sha in result["sha"].items():
                print(f"  {name}: {sha}")
            if not args.validate_only:
                print(f"JSON report: {result['report_json']}")
                print(f"Markdown report: {result['report_markdown']}")
        return 0
    except (OSError, ValueError, MaintenanceError, RepoMapError) as exc:
        print(f"maintenance: {type(exc).__name__}: {str(exc)[:600]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
