# The projects layer: plans, contracts, Gantt, evidence

`lazytools.projects` keeps shared project state in a LazyBridge `Store`:
project records, a plan on a `DurableBlackboard`, task schedules, claims,
contracts, verification records and notes. The same operations are exposed
as `ProjectsTools` and through the `projects` MCP provider
(`lazytools-mcp projects ...`). Full reference: LazyTools `docs/projects.md`.

The project layer records decisions; it never merges or releases code. The
lead decides who may accept work and what checks must prove.

## 1. Open a project and a plan

```python
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard
from lazytools.projects import contracts, notes, records

store = Store(db="projects.sqlite")
records.open_project(store, project_id="svc-retry", title="Retry policy for the export service",
                     objective="Exports retry transient failures and never duplicate a file")
board = DurableBlackboard(store, plan_id="project:svc-retry")
board.set_plan("Codex writes, Claude reviews; the lead runs the checks",
               ["Implement retry with idempotency key", "Review and run integration checks"])
notes.add_project_note(store, "svc-retry", "Integration tests use recorded responses; no live endpoint in CI.")
```

Projects open as drafts. `intake.check_project_intake` wants an observable
result, a deadline and at least two subtasks with falsifiable acceptance
criteria; after an independent review, `promote_project_plan` installs the
reviewed plan and opens the project (MCP: `projects_review_plan`,
`projects_promote`). `plan_edit` revises, retires and reopens tasks without
rewriting started work.

## 2. Contract before delegation

```python
contracts.open_task_contract(
    store, project_id="svc-retry", task_index=0, repo="owner/repo",
    observable_result="Export retries transient errors with an idempotency key",
    acceptance_criteria=["3 retries with backoff on 5xx", "no duplicate file on retry"],
    required_checks=["python -m pytest -q", "ruff check src tests"], risk="medium",
    allowed_effects="Only the export module and its tests",
)
```

`required_checks` are the exact command strings that `close-task` will later
demand. Review is required by default and cannot be waived on high-risk
contracts. Write the same checks into the brief's Done-when section.

## 3. Schedule and Gantt

Dates need an explicit UTC offset (`2026-11-20T18:00+00:00` or `Z`); naive
and date-only values are refused. State every time with its zone, and take
"now" from the OS clock, not from a shell `TZ` variable.

```python
from datetime import UTC, datetime, timedelta
from lazytools.projects import gantt

now = datetime.now(UTC)
board.set_task_schedule(0, "Implement retry with idempotency key", planned_start_at=now.timestamp(),
                        due_at=(now + timedelta(days=1)).timestamp(), reason="Initial schedule")
print(gantt.render_gantt_text(store, statuses=None, now=now))
```

```text
python -m lazytools.projects.gantt --store projects.sqlite --out gantt.html --all-statuses --text
```

MCP equivalents: `projects_set_deadline`, `projects_schedule_task`,
`projects_gantt`, `projects_timeline`, `projects_board_summary`.
The renderer is deterministic (no LLM, no network). **Update the plan and
Gantt at each milestone** — a stale plan misleads the next session.

## 4. Claim, delegate, verify

Claim the board slot before delegating:
`board.claim_task(0, "Implement retry with idempotency key", owner="codex-writer")`.
Then brief and launch the writer, verify, and run the opposite-engine review
(`code-bridge.md`, `routing-and-review.md`).

## 5. Close with evidence

When the checks have run and the independent review is in hand:

```text
lazytools-projects close-task svc-retry 0 \
    --check "python -m pytest -q=pass:214 passed" \
    --check "ruff check src tests=pass" \
    --review "Read the diff at <head sha>; no defects; one nit deferred to task 2." \
    --review-by claude --reason "All required checks pass; independent review clean" \
    --reviewer lead --worker codex-writer --db projects.sqlite
```

- `--check` is `COMMAND=pass|fail[:DETAIL]`, repeated once per contract
  `required_check`; COMMAND must match the contract string exactly.
- Worker, `--review-by` (who reviewed) and `--reviewer` (who accepts) must be
  three different identities. The worker defaults to the claim owner.
- It refuses — writing nothing — on a missing or failed check, a missing
  required review, or overlapping identities. A review given as
  `REVIEW NOT PERFORMED (reason)` records a blocked verification and keeps
  the task open.
- The Store is `--db`, else `$LAZYTOOLS_PROJECTS_STORE_DB`. Exit 0 closed (or
  already done), 1 refused, 2 usage error.
- Same operation in Python: `closure.close_task_with_evidence(...)`; over
  MCP: `projects_close_task`.

The checks are attested by the caller, not executed by the command: record
only results you actually ran and read in full.

## Brake and quota admission

Delegated work attributed to a project passes `admission.project_admit`
when the project's brake is on (default): fresh telemetry, ceilings,
forecast. Bridge `route` / `run --tier` are operator-directed and have no
project attribution; connect bridge job ids to contracts yourself.

## Building multi-step pipelines

When the work itself is a repeatable pipeline (stages, resume, persistence),
build it as a LazyBridge `Plan` of `Step`s rather than ad-hoc scripts, and
ship it with a `MockAgent` composition test — see the `lazybridge` skill's
`references/plan-and-pipelines.md`.
