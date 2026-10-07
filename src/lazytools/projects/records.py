"""Durable registry for independent pieces of ongoing work.

Ported from ``lazyceo.projects`` (branch ``feat/adopt-lazybridge-ext``),
mechanism only. Left behind, as CEO POLICY: ``autonomy_level`` (per-project
autonomy levels are LazyCEO's own concept) and ``paused_specialists``
(specialist lifecycle is explicitly out of scope for this package -- see
``lazyceo.project_work``, which stays in LazyCEO). Both are CEO-only fields
on the *same* stored record this module also writes, which is exactly why
:class:`ProjectRecord` below declares ``model_config = ConfigDict(extra="allow")``:
a record an existing LazyCEO install already wrote carries those two fields,
and this module's ``_apply`` (the shared compare-and-swap helper every
mutator here goes through) must round-trip them unchanged rather than
silently drop them -- the inverse of the hazard ``lazyceo.projects`` itself
documents about an *old* model reading a *newer* record. See
docs/projects.md's concurrency section.

``size``/``risk``/``process`` follow the same size/risk/process vocabulary
documented in this ecosystem's shared ``task-and-delegation-discipline``
skill (small/medium/large sizing, low/medium/high risk, inline/delegate/
staged process) -- not a LazyCEO-only idea, so it stays here as mechanism.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

from lazytools.projects.keys import PROJECT_PREFIX
from lazytools.projects.owner import get_project_owner

if TYPE_CHECKING:
    from lazybridge import Store

ProjectStatus = Literal["draft", "open", "paused", "done"]

_SIZE_ALIASES = {"small": "small", "low": "small", "medium": "medium", "large": "large", "high": "large"}
_VALID_RISKS = ("low", "medium", "high")
_VALID_PROCESSES = ("inline", "delegate", "staged")
_TIER_RANK = {"small": 0, "low": 0, "medium": 1, "large": 2, "high": 2}
_PROCESS_RANK = {process: rank for rank, process in enumerate(_VALID_PROCESSES)}

#: Same key-safe identity constraint LazyCEO uses for project_id / specialist names.
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


class ProjectRecord(BaseModel):
    """A durable project. See the module docstring for ``extra="allow"``."""

    model_config = ConfigDict(extra="allow")

    project_id: str
    title: str
    objective: str
    status: ProjectStatus
    created_at: datetime
    last_progress_at: datetime | None = None
    size: str | None = None
    risk: str | None = None
    process: str | None = None
    classification_rationale: str | None = None
    target_completion_at: datetime | None = None
    schedule_timezone: str | None = None
    #: What a person could look at to see this is finished.
    observable_result: str | None = None
    #: The digest of the plan an independent reviewer actually saw -- see ``intake.py``.
    reviewed_digest: str | None = None
    #: The criteria put in front of that reviewer.
    acceptance_criteria: list[str] = []


def _key(project_id: str, *, prefix: str = PROJECT_PREFIX) -> str:
    return f"{prefix}{project_id}"


def validate_project_id(project_id: str) -> None:
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"project_id must match {_PROJECT_ID_RE.pattern} (lowercase letters/digits/hyphens, starting with a letter)"
        )


def open_project(
    store: Store,
    *,
    project_id: str,
    title: str,
    objective: str,
    size: str | None = None,
    risk: str | None = None,
    process: str | None = None,
    classification_rationale: str | None = None,
    prefix: str = PROJECT_PREFIX,
) -> ProjectRecord:
    """Create a project AS A DRAFT, rejecting any existing record with the same id.

    Validated before the create-only CAS: this API has no edit or delete, so
    a bad value would permanently consume the id. Classification
    (size/risk/process) is optional, but once any one is set all three must
    be, and ``process`` must be at least as strong as the higher of
    size/risk demands (the sizing discipline's own rule).
    """
    validate_project_id(project_id)
    if not title.strip():
        raise ValueError("title must not be blank")
    if not objective.strip():
        raise ValueError("objective must not be blank")
    if size is not None:
        if size not in _SIZE_ALIASES:
            raise ValueError(f"size must be one of {sorted(set(_SIZE_ALIASES))} (got {size!r})")
        size = _SIZE_ALIASES[size]
    if risk is not None and risk not in _VALID_RISKS:
        raise ValueError(f"risk must be one of {list(_VALID_RISKS)} (got {risk!r})")
    if process is not None and process not in _VALID_PROCESSES:
        raise ValueError(f"process must be one of {list(_VALID_PROCESSES)} (got {process!r})")
    if size is not None or risk is not None or process is not None or classification_rationale is not None:
        if size is None or risk is None or process is None:
            raise ValueError(
                "size, risk, and process must all be provided together once any one of them is "
                f"classified (got size={size!r}, risk={risk!r}, process={process!r})"
            )
        required_rank = max(_TIER_RANK[size], _TIER_RANK[risk])
        if _PROCESS_RANK[process] < required_rank:
            raise ValueError(
                f"process {process!r} is below what size={size!r}/risk={risk!r} requires "
                f"({_VALID_PROCESSES[required_rank]!r} or higher)"
            )
    record = ProjectRecord(
        project_id=project_id,
        title=title,
        objective=objective,
        status="draft",
        created_at=datetime.now(UTC),
        size=size,
        risk=risk,
        process=process,
        classification_rationale=classification_rationale,
    )
    if not store.compare_and_swap(_key(project_id, prefix=prefix), None, record.model_dump(mode="json")):
        raise ValueError(f"project {project_id!r} is already registered -- pick a different project_id")
    return record


def project_classification_suffix(record: ProjectRecord) -> str:
    """Render ``[size=..., risk=..., process=...] -- rationale`` for a record."""
    classification = ", ".join(
        f"{name}={value}"
        for name, value in (("size", record.size), ("risk", record.risk), ("process", record.process))
        if value is not None
    )
    suffix = f" [{classification}]" if classification else ""
    if record.classification_rationale:
        suffix += f" -- {record.classification_rationale}"
    return suffix


def get_project(store: Store, project_id: str, *, prefix: str = PROJECT_PREFIX) -> ProjectRecord | None:
    raw = store.read(_key(project_id, prefix=prefix))
    return ProjectRecord.model_validate(raw) if isinstance(raw, dict) else None


def list_projects(
    store: Store,
    *,
    status: str | None = None,
    owner: str | None = None,
    prefix: str = PROJECT_PREFIX,
) -> list[ProjectRecord]:
    """Matching projects, oldest first, including paused and done records.

    ``owner`` filters by :func:`lazytools.projects.owner.get_project_owner`
    (a project with no explicit owner record reads as ``"ceo"``). ``None``
    (the default) returns every owner -- callers that want Claude Code's own
    default view (``claude`` + ``shared``, read-only visibility of ``ceo``)
    apply that themselves, since what counts as "the default view" is a
    policy choice for the caller (the MCP tool), not this registry read.
    """
    records = [
        ProjectRecord.model_validate(raw) for _key, raw in store.items(prefix=prefix) if isinstance(raw, dict)
    ]
    if status is not None:
        records = [record for record in records if record.status == status]
    if owner is not None:
        records = [record for record in records if get_project_owner(store, record.project_id) == owner]
    records.sort(key=lambda record: record.created_at)
    return records


def _apply(
    store: Store, project_id: str, updates: dict[str, object], *, allowed_from: tuple[str, ...], prefix: str = PROJECT_PREFIX
) -> bool:
    """Compare-and-swap one field change, guarded by the current status.

    Every mutator in this module goes through this one helper -- see the
    module docstring for why :class:`ProjectRecord` is ``extra="allow"``:
    this round-trips a record written by a model that knows MORE fields
    than this one (today, LazyCEO's own ``autonomy_level``/
    ``paused_specialists``) without dropping them.
    """
    key = _key(project_id, prefix=prefix)
    raw = store.read(key)
    if not isinstance(raw, dict):
        return False
    record = ProjectRecord.model_validate(raw)
    if record.status not in allowed_from:
        return False
    updated = record.model_copy(update=updates)
    return store.compare_and_swap(key, raw, updated.model_dump(mode="json"))


def adopt_existing_project(store: Store, *, adoption_reason: str, **kwargs: object) -> ProjectRecord:
    """Create a project that is already running, for work that predates the intake gate."""
    record = open_project(store, **kwargs)  # type: ignore[arg-type]
    _apply(
        store,
        record.project_id,
        {"status": "open", "classification_rationale": record.classification_rationale or adoption_reason},
        allowed_from=("draft",),
    )
    adopted = get_project(store, record.project_id)
    assert adopted is not None
    return adopted


def pause_project(store: Store, project_id: str) -> bool:
    """Pause an open (or already paused) project; a done one stays done.

    Unlike ``lazyceo.projects.pause_project``, this never touches
    specialists or jobs -- that is specialist-lifecycle policy, out of scope
    here (see ``lazyceo.project_work``, which stays in LazyCEO). This is
    pure record bookkeeping.
    """
    return _apply(store, project_id, {"status": "paused"}, allowed_from=("open", "paused"))


def resume_project(store: Store, project_id: str) -> bool:
    """Resume a paused project, or deliberately reopen a done one."""
    return _apply(store, project_id, {"status": "open"}, allowed_from=("paused", "done"))


def close_project(store: Store, project_id: str) -> bool:
    """Mark a known project done; closing twice is successful."""
    return _apply(store, project_id, {"status": "done"}, allowed_from=("draft", "open", "paused", "done"))


def set_project_deadline(
    store: Store,
    project_id: str,
    *,
    target_completion_at: datetime | None,
    schedule_timezone: str | None = None,
) -> bool:
    """Set or clear a project's deadline. ``target_completion_at`` must be timezone-aware."""
    if target_completion_at is not None and (
        target_completion_at.tzinfo is None or target_completion_at.utcoffset() is None
    ):
        raise ValueError("target_completion_at must be timezone-aware; naive datetimes are not accepted")
    if schedule_timezone is not None:
        try:
            ZoneInfo(schedule_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"schedule_timezone must be a valid IANA timezone name (got {schedule_timezone!r})"
            ) from exc
    return _apply(
        store,
        project_id,
        {"target_completion_at": target_completion_at, "schedule_timezone": schedule_timezone},
        allowed_from=("open", "paused", "done"),
    )


def touch_project_progress(store: Store, project_id: str) -> None:
    """Best-effort progress bookkeeping that never interrupts real work.

    Only an open project records progress -- see ``lazyceo.projects``'s own
    docstring for why a paused/done project must not be restamped.
    """
    try:
        _apply(store, project_id, {"last_progress_at": datetime.now(UTC)}, allowed_from=("open",))
    except Exception:
        return


__all__ = [
    "ProjectRecord",
    "ProjectStatus",
    "adopt_existing_project",
    "close_project",
    "get_project",
    "list_projects",
    "open_project",
    "pause_project",
    "project_classification_suffix",
    "resume_project",
    "set_project_deadline",
    "touch_project_progress",
    "validate_project_id",
]
