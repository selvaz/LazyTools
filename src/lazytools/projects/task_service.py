"""The governed queue over the existing projects board and acceptance records."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, FiniteFloat, StringConstraints
from typing_extensions import Annotated

from lazytools.projects.plan_edit import _board_key
from lazytools.projects.records import get_project
from lazytools.projects.task_fields import Placement, task_id
from lazytools.projects.task_store import TaskServiceError, TransactionStore, transaction

Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TaskPrincipal(BaseModel):
    """Identity and project scope supplied by the trusted host."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    actor: Nonempty
    role: Literal["worker", "orchestrator", "owner"]
    projects: tuple[Nonempty, ...]

    def require_project(self, project_id: str) -> None:
        if project_id not in self.projects:
            raise TaskServiceError("forbidden", "Project is outside the authenticated scope")


class PlacementEvidence(BaseModel):
    """A trusted runtime check for one placement and one task revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    placement: Placement
    eligible: bool
    reason: Nonempty
    reference: Nonempty
    expires_at: FiniteFloat
    engine_family: Nonempty


@dataclass
class BoardTask:
    project_id: str
    board: dict[str, Any]
    index: int

    @property
    def data(self) -> dict[str, Any]:
        return self.board["tasks"][self.index]

    @property
    def identifier(self) -> str:
        return f"{self.project_id}/{task_id(self.data, self.index)}"

    @property
    def revision(self) -> str:
        return self.data.get("revision", "0")

    def save(self, store: TransactionStore, now: float) -> None:
        self.board["updated_at"] = now
        store.write(_board_key(self.project_id), self.board)


def _load_task(store: TransactionStore, identifier: str) -> BoardTask:
    project_id, separator, identifier_in_project = identifier.partition("/")
    board = store.read(_board_key(project_id)) if separator and identifier_in_project else None
    if board is not None:
        for index, task in enumerate(board["tasks"]):
            if task_id(task, index) == identifier_in_project:
                return BoardTask(project_id, board, index)
    raise TaskServiceError("not_found", "Task does not exist")


def _dependencies(task: BoardTask) -> list[dict[str, Any]]:
    by_id = {task_id(item, index): item for index, item in enumerate(task.board["tasks"])}
    return [
        {"task_id": f"{task.project_id}/{dependency}", "status": by_id.get(dependency, {}).get("status", "missing")}
        for dependency in task.data.get("dependency_ids", [])
    ]


def _placement(task: BoardTask, now: float) -> tuple[PlacementEvidence | None, str]:
    requested = task.data.get("requested_placement", "local")
    evidence = task.data.get("placement_evidence", {})

    def check(location: Placement) -> tuple[PlacementEvidence | None, str]:
        record = evidence.get(location)
        if record is None or record["revision"] != task.revision:
            return None, f"{location} placement evidence is missing"
        current = PlacementEvidence.model_validate(record["evidence"])
        if current.expires_at <= now:
            return None, f"{location} placement evidence is stale"
        return current if current.eligible else None, current.reason

    eligible, reason = check(requested)
    if eligible is None and requested == "cloud":
        eligible, local_reason = check("local")
        reason = f"{reason}. Local fallback: {local_reason}"
    return eligible, reason


def _ready(task: BoardTask, now: float) -> tuple[dict[str, Any], PlacementEvidence] | None:
    data = task.data
    if data.get("status") != "todo" or data.get("owner") or data.get("claimed_at") is not None or data.get("start_hold"):
        return None
    dependencies = _dependencies(task)
    if any(dep["status"] != "done" for dep in dependencies):
        return None
    placement, reason = _placement(task, now)
    if placement is None:
        return None
    return {
        "task_id": task.identifier,
        "project_id": task.project_id,
        "revision": task.revision,
        "text": data["text"],
        "tier": data.get("tier"),
        "dependencies": dependencies,
        "acceptance_criteria": data.get("acceptance_criteria", []),
        "requested_profile": data.get("requested_profile"),
        "requested_network": data.get("requested_network"),
        "requested_placement": data.get("requested_placement", "local"),
        "effective_placement": placement.placement,
        "placement_reason": reason,
        "preflight_ref": placement.reference,
    }, placement


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise TaskServiceError("invalid", "Limit must be between 1 and 100")


class ProjectTaskService:
    def __init__(self, store_db: str | None, *, lease_seconds: int = 900):
        self.path = Path(store_db).resolve() if store_db and store_db != ":memory:" else None
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.lease_seconds = lease_seconds

    def record_placement(self, task_id: str, revision: str, evidence: PlacementEvidence) -> None:
        """Persist an executor's preflight/runtime observation. Host operation."""
        with transaction(self.path, write=True) as store:
            task = _load_task(store, task_id)
            if task.revision != revision:
                raise TaskServiceError("stale", "Task revision changed")
            task.data.setdefault("placement_evidence", {})[evidence.placement] = {
                "revision": revision,
                "evidence": evidence.model_dump(mode="json"),
            }
            task.save(store, time.time())

    def next_ready(
        self, principal: TaskPrincipal, project_id: str, limit: int = 20, cursor: int | None = None
    ) -> dict[str, Any]:
        principal.require_project(project_id)
        _validate_limit(limit)
        if cursor is not None and cursor < 0:
            raise TaskServiceError("invalid", "Cursor must be a nonnegative task index")
        with transaction(self.path) as store:
            project = get_project(store, project_id)
            board = store.read(_board_key(project_id))
            if project is None or project.status != "open" or board is None:
                return {"tasks": [], "next_cursor": None}
            now = time.time()
            items = []
            next_cursor = None
            for index in range(0 if cursor is None else cursor + 1, len(board["tasks"])):
                ready = _ready(BoardTask(project_id, board, index), now)
                if ready is not None:
                    if len(items) == limit:
                        next_cursor = last_index
                        break
                    items.append(ready[0])
                    last_index = index
            return {"tasks": items, "next_cursor": next_cursor}
