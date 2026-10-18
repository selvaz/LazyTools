"""One-off, idempotent backfill of the board ``contract_id`` marker.

``contracts.open_task_contract`` now stamps ``task["contract_id"]`` on the
project board in a board CAS, and ``task_claims.complete_todo_without_verification``
refuses on that marker inside its own board CAS (that is what makes the gate
atomic). Contracts created BEFORE that change have no marker; until they get
one, only the non-atomic ``find_contract_for_task`` lookup protects them.

This module stamps the marker for those contracts. It is safe to run against a
live store and to run repeatedly:

* a task that already carries a ``contract_id`` is left alone (never overwritten);
* when several contracts exist for one task, the newest (by ``created_at``) wins,
  matching ``find_contract_for_task``;
* a contract whose project has no board, or whose ``task_index`` is outside the
  board, is reported and skipped;
* every write is a board CAS that re-reads first, so it cannot clobber a
  concurrent writer; a task that is already ``done`` is marked too (harmless).

Default is a DRY RUN; pass ``--apply`` to write. The owner's session runs it::

    C:\\ProgramData\\spyder-6\\python.exe -m lazytools.projects.contract_markers --store-db <path-to-ceo-store.sqlite>
    C:\\ProgramData\\spyder-6\\python.exe -m lazytools.projects.contract_markers --store-db <path-to-ceo-store.sqlite> --apply
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lazytools.projects.contracts import TaskContract, mark_task_has_contract
from lazytools.projects.keys import BOARD_KEY_PREFIX, TASK_CONTRACT_PREFIX

if TYPE_CHECKING:
    from lazybridge import Store


@dataclass
class BackfillReport:
    """What the backfill did (or, in a dry run, would do), as ``project:index`` labels."""

    marked: list[str] = field(default_factory=list)
    already_marked: list[str] = field(default_factory=list)
    skipped_no_board_task: list[str] = field(default_factory=list)
    applied: bool = False

    def summary(self) -> str:
        verb = "marked" if self.applied else "would mark"
        return (
            f"{verb} {len(self.marked)}, already marked {len(self.already_marked)}, "
            f"skipped (no board task) {len(self.skipped_no_board_task)}"
        )


def backfill_contract_markers(
    store: Store,
    *,
    apply: bool = False,
    prefix: str = TASK_CONTRACT_PREFIX,
    board_prefix: str = BOARD_KEY_PREFIX,
) -> BackfillReport:
    """Stamp ``contract_id`` on every board task that has a contract but no marker."""
    newest: dict[tuple[str, int], TaskContract] = {}
    for _key, raw in store.items(prefix=prefix):
        if not isinstance(raw, dict):
            continue
        contract = TaskContract.model_validate(raw)
        slot = (contract.project_id, contract.task_index)
        if slot not in newest or contract.created_at > newest[slot].created_at:
            newest[slot] = contract

    report = BackfillReport(applied=apply)
    for (project_id, task_index), contract in sorted(newest.items()):
        label = f"{project_id}:{task_index}"
        board = store.read(f"{board_prefix}project:{project_id}")
        tasks = board.get("tasks") if isinstance(board, dict) else None
        if not isinstance(tasks, list) or not 0 <= task_index < len(tasks):
            report.skipped_no_board_task.append(label)
        elif tasks[task_index].get("contract_id"):
            report.already_marked.append(label)
        elif apply:
            # Re-reads and CASes the board; a marker stamped concurrently (e.g.
            # by a brand-new contract) is left alone, never overwritten.
            mark_task_has_contract(
                store, project_id, task_index, contract.contract_id, board_prefix=board_prefix, allow_done=True, only_if_unmarked=True
            )
            report.marked.append(label)
        else:
            report.marked.append(label)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill the board contract_id marker for pre-existing task contracts.")
    parser.add_argument("--store-db", required=True, help="path to the projects/CEO store sqlite file")
    parser.add_argument("--apply", action="store_true", help="write the markers (default: dry run)")
    args = parser.parse_args(argv)

    from lazybridge import Store

    store = Store(db=args.store_db)
    report = backfill_contract_markers(store, apply=args.apply)
    for label in report.marked:
        print(f"{'marked' if report.applied else 'would mark'}: {label}")
    for label in report.skipped_no_board_task:
        print(f"skipped (no board task): {label}")
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
