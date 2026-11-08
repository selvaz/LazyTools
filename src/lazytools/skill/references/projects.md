# Projects, plans and schedules

Configure a persistent Store and enable the `projects` MCP provider with `--allow-unsafe` for changes (see [MCP setup](mcp-server.md)). Read tools remain available without it. Enable `LAZYTOOLS_PROJECTS_TOOLS=full` for contract and verification tools.

## MCP workflow

1. Call `projects_create` with `project_id`, `title`, and `objective`; the project starts as a draft.
2. Call `projects_review_plan` with an observable result, an offset-aware deadline, and at least two subtasks, each with text and falsifiable acceptance criteria. Resolve findings, then call `projects_promote` with the same plan arguments.
3. Read `projects_schedule` for each task's index, full text, and dates. Call `projects_schedule_task` with the index, exact `expected_text`, a reason, and offset-aware `planned_start_at` / `due_at`. `projects_gantt` returns text and an HTML path; `projects_timeline` gives a timeline. Task dates drive Gantt bars.
4. For contracted work use the full-profile contract and verification tools, including `projects_open_contract`, `projects_accept_verification`, and `projects_close_accepted_task`. Check the results before treating work as complete. Claims and completion of an uncontracted todo are available through Python below.

Quick update: read `projects_schedule`; verify evidence and finish eligible uncontracted todos via Python, or close contracted work through verification. Add tasks with `projects_revise_plan`, passing all open indexes in `keep` (omitted indexes are retired), then schedule using the exact text. Add context with `projects_add_note` and show `projects_gantt`. Re-read and retry on a concurrency conflict.

## Python API

```python
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard
from lazytools.projects import gantt, records, schedule
from lazytools.projects.task_claims import complete_todo_without_verification

store = Store(db="projects.sqlite")
records.open_project(store, project_id="demo", title="Demo", objective="Deliver a tested feature")
board = DurableBlackboard(store, plan_id="project:demo")
board.set_plan("Implement and review", ["Implement feature", "Review feature"])
board.set_task_schedule(0, "Implement feature", planned_start_at=1800000000, due_at=1800086400, reason="Plan")
print(schedule.project_schedule_view(store, "demo"))
print(gantt.render_gantt_text(store, statuses=None))
# Claim a task before starting; inspect the returned claim/refusal.
print(board.claim_task(0, "Implement feature", owner="worker"))
# A separate unclaimed todo without a contract or running job can be closed directly:
print(complete_todo_without_verification(store, "demo", 1, "Reviewed; evidence: <commit>"))
```

The direct board example leaves the project as a draft; production plan promotion uses the intake review flow. A claimed task and a task with a contract cannot use the unclaimed-todo closer; contracted work closes through acceptance. See the public projects documentation for verification and claim lifecycle details. The Store shared by the API and MCP must point at the same file to share state.
