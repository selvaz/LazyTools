"""Reusable project-management mechanism, shared by LazyCEO and Claude Code.

This package is the generic MECHANISM half of what used to live only inside
LazyCEO (``lazyceo.projects``, ``.project_schedule``, ``.project_timeline``,
``.plan_edit``, ``.task_claims``, ``.verification``, ``.quota``,
``.admission``, ``.cost_report``): a durable project registry, a plan-editing
layer over LazyBridge's ``DurableBlackboard``, a task acceptance-contract /
verification loop, and an engine quota brake. POLICY that used to sit next to
that mechanism -- per-project autonomy levels, Telegram, specialist lifecycle,
the operator-approval ticket flow, the exact wording of LazyCEO's own intake
checklist -- stays in LazyCEO; this package exposes injectable hooks (plain
callables, plain booleans) wherever LazyCEO's policy used to be reached for
directly.

Every function here takes a ``lazybridge.Store`` explicitly and a project
registry is just a set of keys in it, under configurable prefixes that
default to exactly what LazyCEO writes today -- see ``keys.py``. Running this
package against the SAME Store LazyCEO writes means both see the same
projects; see ``docs/projects.md`` for the concurrency rules that come with
that.

See ``docs/projects.md`` for the full design: the owner model
(``ceo``/``claude``/``shared``), the per-project quota brake switch, and
which write tools are safe to use while a live CEO process is also writing.
"""

from __future__ import annotations

from lazytools.projects.admission import (
    AdmissionDecision,
    EngineBudget,
    admit,
    budget_for,
    decide,
    in_flight_count,
    preflight,
    project_admit,
    release,
    shadow_findings,
    under_plan_warning,
)
from lazytools.projects.brake import get_project_brake_enabled, set_project_brake_enabled
from lazytools.projects.contracts import (
    TaskContract,
    check_uses_exclusion_flag,
    contract_requires_full_suite,
    diff_is_empty,
    diff_is_empty_snapshot,
    find_contract_for_task,
    get_task_contract,
    open_task_contract,
    repos_for_project,
    resolve_review_base,
    snapshot_repo_state,
)
from lazytools.projects.cost_report import project_cost_report, project_jobs
from lazytools.projects.intake import (
    IntakeVerdict,
    check_project_intake,
    parse_deadline,
    plan_digest,
    promote_project_plan,
    promotion_refusal,
    record_project_review,
)
from lazytools.projects.notes import add_project_note, project_board_summary, recent_project_notes
from lazytools.projects.owner import ProjectOwner, get_project_owner, list_project_ids_by_owner, set_project_owner
from lazytools.projects.plan_edit import (
    MAX_AUTONOMOUS_REOPENS,
    MIN_REOPEN_REASON_CHARS,
    open_indexes,
    reopen_budget_left,
    reopen_done_task_for_invalid_closure,
    reopen_task,
    retire_task,
    revise_plan,
    set_task_schedule,
)
from lazytools.projects.quota_telemetry import (
    CACHE_SECONDS,
    TelemetryReading,
    WindowReading,
    cache_quota_reading,
    forget_cached_quota,
    read_quota,
    read_quota_sync,
)
from lazytools.projects.records import (
    ProjectRecord,
    ProjectStatus,
    adopt_existing_project,
    close_project,
    get_project,
    list_projects,
    open_project,
    pause_project,
    project_classification_suffix,
    resume_project,
    set_project_deadline,
    touch_project_progress,
    validate_project_id,
)
from lazytools.projects.schedule import (
    ProjectScheduleStatus,
    ProjectScheduleTask,
    ProjectScheduleView,
    ScheduleState,
    project_schedule_status,
    project_schedule_view,
)
from lazytools.projects.task_claims import complete_todo_without_verification, release_claim
from lazytools.projects.timeline import render_project_timeline
from lazytools.projects.verification import (
    CheckResult,
    ReviewRecord,
    Verification,
    VerificationStatus,
    accept,
    block,
    claim_blocker,
    claim_verification,
    get_verification,
    get_verification_for_contract,
    reclaim_interrupted_verifications,
    record_checks,
    record_review,
    reopen_for_empty_review,
    request_rework,
    retry_harness,
    retry_review,
    start_running,
)

__all__ = [
    "AdmissionDecision",
    "CACHE_SECONDS",
    "CheckResult",
    "EngineBudget",
    "IntakeVerdict",
    "MAX_AUTONOMOUS_REOPENS",
    "MIN_REOPEN_REASON_CHARS",
    "ProjectOwner",
    "ProjectRecord",
    "ProjectScheduleStatus",
    "ProjectScheduleTask",
    "ProjectScheduleView",
    "ProjectStatus",
    "ReviewRecord",
    "ScheduleState",
    "TaskContract",
    "TelemetryReading",
    "Verification",
    "VerificationStatus",
    "WindowReading",
    "accept",
    "add_project_note",
    "admit",
    "adopt_existing_project",
    "block",
    "budget_for",
    "cache_quota_reading",
    "check_project_intake",
    "check_uses_exclusion_flag",
    "claim_blocker",
    "claim_verification",
    "close_project",
    "contract_requires_full_suite",
    "decide",
    "diff_is_empty",
    "diff_is_empty_snapshot",
    "complete_todo_without_verification",
    "find_contract_for_task",
    "forget_cached_quota",
    "get_project",
    "get_project_brake_enabled",
    "get_project_owner",
    "get_task_contract",
    "get_verification",
    "get_verification_for_contract",
    "in_flight_count",
    "list_project_ids_by_owner",
    "list_projects",
    "open_indexes",
    "open_project",
    "open_task_contract",
    "parse_deadline",
    "pause_project",
    "plan_digest",
    "preflight",
    "project_admit",
    "project_board_summary",
    "project_classification_suffix",
    "project_cost_report",
    "project_jobs",
    "project_schedule_status",
    "project_schedule_view",
    "promote_project_plan",
    "promotion_refusal",
    "read_quota",
    "read_quota_sync",
    "reclaim_interrupted_verifications",
    "record_checks",
    "record_project_review",
    "record_review",
    "release",
    "release_claim",
    "recent_project_notes",
    "render_project_timeline",
    "reopen_budget_left",
    "reopen_done_task_for_invalid_closure",
    "reopen_for_empty_review",
    "reopen_task",
    "repos_for_project",
    "request_rework",
    "resolve_review_base",
    "resume_project",
    "retire_task",
    "retry_harness",
    "retry_review",
    "revise_plan",
    "set_project_brake_enabled",
    "set_project_deadline",
    "set_project_owner",
    "set_task_schedule",
    "shadow_findings",
    "snapshot_repo_state",
    "start_running",
    "touch_project_progress",
    "under_plan_warning",
    "validate_project_id",
]
