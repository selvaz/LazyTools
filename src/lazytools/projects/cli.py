"""``lazytools-projects``: command-line verbs over the project layer.

Currently one subcommand::

    lazytools-projects close-task <project_id> <task_index> \\
        --check "python -m pytest tests -q=pass:412 passed" \\
        --check "ruff check src=pass" \\
        --review "Read the diff; no issues." --reason "Checks and review pass" \\
        --reviewer claude --db <store.sqlite>

``--check`` is ``COMMAND=pass|fail[:DETAIL]`` and repeatable; COMMAND must be a
contract ``required_check`` string exactly. The Store is ``--db``, else
``$LAZYTOOLS_PROJECTS_STORE_DB``, else ``$LAZYCEO_CEO_STORE_DB``; with none of
them the command refuses (an in-memory store would close nothing).

Exit status: 0 when the task was closed or already done, 1 on a refusal,
2 on a usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from lazytools.projects.closure import CheckEvidence, close_task_with_evidence
from lazytools.projects.keys import CEO_STORE_DB_ENV

_ENV_STORE_DB = "LAZYTOOLS_PROJECTS_STORE_DB"

# Non-greedy command: the FIRST "=pass"/"=fail" followed by ":" or the end
# splits command from status, so a detail may contain "=" freely.
_CHECK_RE = re.compile(r"^(?P<command>.+?)=(?P<status>pass|fail)(?::(?P<detail>.*))?$", re.DOTALL)


def parse_check(value: str) -> CheckEvidence:
    """``"COMMAND=pass|fail[:DETAIL]"`` -> ``(command, passed, detail)``."""
    match = _CHECK_RE.match(value)
    if match is None or not match.group("command").strip():
        raise argparse.ArgumentTypeError(f"--check must look like 'COMMAND=pass|fail[:DETAIL]', got {value!r}")
    return match.group("command").strip(), match.group("status") == "pass", (match.group("detail") or "").strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lazytools-projects", description="Project-layer commands.")
    sub = parser.add_subparsers(dest="command", required=True)
    close = sub.add_parser(
        "close-task",
        help="Close a contract-backed task with evidence (checks + review).",
        description="Close a contract-backed task: claim, record checks and review, accept, close the board slot.",
    )
    close.add_argument("project_id")
    close.add_argument("task_index", type=int)
    close.add_argument(
        "--check",
        dest="checks",
        action="append",
        type=parse_check,
        default=[],
        metavar="COMMAND=pass|fail[:DETAIL]",
        help="One check's result; repeat for each contract required_check.",
    )
    close.add_argument("--review", default="", help="The independent review's findings.")
    close.add_argument("--reason", required=True, help="Why this evidence is enough to close the task.")
    close.add_argument("--reviewer", required=True, help="Who is accepting.")
    close.add_argument("--db", default=None, help=f"Store path (default: ${_ENV_STORE_DB}, then ${CEO_STORE_DB_ENV}).")
    return parser


def _resolve_db(db: str | None) -> str | None:
    return db or os.environ.get(_ENV_STORE_DB) or os.environ.get(CEO_STORE_DB_ENV) or None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = _resolve_db(args.db)
    if db is None:
        print(f"REJECTED: no store -- pass --db or set ${_ENV_STORE_DB}.", file=sys.stderr)
        return 2
    if not Path(db).is_file():
        print(f"REJECTED: store {db!r} does not exist.", file=sys.stderr)
        return 2

    from lazybridge import Store

    store = Store(db=db)
    result = close_task_with_evidence(
        store,
        args.project_id,
        args.task_index,
        checks=args.checks,
        review=args.review,
        reason=args.reason,
        reviewer=args.reviewer,
    )
    refused = result.startswith("REJECTED")
    print(result, file=sys.stderr if refused else sys.stdout)
    return 1 if refused else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
