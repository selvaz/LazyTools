# Projects: shared state for collaborative work

`lazytools.projects` connects Claude Code and Codex work through a LazyBridge
Store: project records, plans on `DurableBlackboard`, schedule dates, claims,
contracts, verification, notes, cost reports and quota admission. The same
mechanisms are exposed through `ProjectsTools` and the `projects` MCP provider.
LazyTools 0.10.1 is Alpha. Caller policy chooses who may accept work and what
the checks must prove; project bookkeeping does not merge or release code.

## Records, owner and plans

Run this in a fresh demo folder. `Store(db="projects.sqlite")` persists;
`Store()` is in-memory. Projects open as drafts. The example deliberately
keeps the draft until its intake plan has been independently reviewed.

```python
from lazybridge import Store
from lazybridge.ext.planners import DurableBlackboard
from lazytools.projects import brake, contracts, notes, owner, records

store = Store(db="projects.sqlite")
project = records.open_project(
    store, project_id="deployment", title="Weekday deployment guard",
    objective="Block weekend deployment and verify the Friday-to-Monday boundary",
)
owner.set_project_owner(store, "deployment", "shared")
board = DurableBlackboard(store, plan_id="project:deployment")
board.set_plan("Claude writes, Codex reviews; code executes the checks",
               ["Implement the guard", "Review and run boundary checks"])
notes.add_project_note(store, "deployment", "Keep test_deployment.py independent of the writer.")
contract = contracts.open_task_contract(
    store, project_id="deployment", task_index=0, repo="toy-tandem",
    observable_result="Weekday deployment guard with independent boundary tests",
    acceptance_criteria=["Friday and Monday allowed; Saturday and Sunday blocked"],
    required_checks=["python -m unittest discover -v"], risk="medium",
    allowed_effects="Only deployment.py; preserve the independent boundary test",
)
assert brake.get_project_brake_enabled(store, "deployment") is True
print(records.get_project(store, "deployment"))
```

Owners `ceo`, `claude`, `shared` are coordination labels, not ACLs. Missing
owner metadata defaults to `ceo`. Owner and brake flags have separate keys so
record updates do not overwrite them. `records.list_projects(owner=...)`
filters by owner; MCP additionally accepts `default` (`claude` + `shared`)
and `all`.

For promotion, `intake.check_project_intake` checks an observable result,
deadline and at least two subtasks with falsifiable acceptance criteria.
`plan_digest` identifies exactly the reviewed plan. After a real review,
`record_project_review` records that digest and `promote_project_plan` installs
it and opens the project. `review_and_promote_project_plan` accepts an injected
async reviewer returning `(findings, performed)`; findings or an unperformed
review leave the project a draft. Started tasks prevent silent plan replacement.
`install_project_plan` uses whole-board compare-and-swap; identical plans are
no-ops. `plan_edit` provides revision, retire and reopen operations without
rewriting active work as untouched tasks.

## Schedule and deterministic Gantt

Continue the example with explicit UTC dates. The project deadline and each
task window are separate records; one does not infer the other.

```python
from datetime import UTC, datetime, timedelta
from lazytools.projects import gantt, schedule

now = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
board.set_task_schedule(0, "Implement the guard", planned_start_at=now.timestamp(),
                        due_at=(now + timedelta(days=1)).timestamp(), reason="Demo schedule")
board.set_task_schedule(1, "Review and run boundary checks",
                        planned_start_at=(now + timedelta(days=1)).timestamp(),
                        due_at=(now + timedelta(days=2)).timestamp(), reason="After writer")
print(schedule.project_schedule_view(store, "deployment", now=now).schedule_status)
page = gantt.render_gantt_html(store, statuses=None, now=now)
assert page == gantt.render_gantt_html(store, statuses=None, now=now)
gantt.write_gantt_html(store, "gantt.html", statuses=None, now=now)
print(gantt.render_gantt_text(store, statuses=None, now=now))
```

The renderer now uses **English fixed labels** in HTML and text; user task
text remains unchanged and HTML-escaped. Bars come from recorded dates, with
completion marks, today, overdue/failed tasks and hold markers. It uses no LLM
and no network. Given the same records, filters and fixed `now`, rendering is
byte-identical. This is recorded scheduling, not predictive planning.

```sh
python -m lazytools.projects.gantt --store projects.sqlite --out gantt.html --all-statuses --text
```

CLI defaults draw open/paused projects; `--all-statuses` includes this draft.
`--owner` can repeat; `--unplanned-done` adds completion-only tasks otherwise
counted but not drawn. The CLI reads the current time; use the Python `now`
argument for reproducible snapshots. `projects_gantt` exposes the same renderer
through MCP. A caller-chosen output path requires write-enabled tools.

## Claims

Claim the exact board slot and expected text before delegating:
`board.claim_task(0, "Implement the guard", owner="claude-writer")`.
A caller should check the returned claim/refusal, keep its lease alive and
connect its job/contract to that slot. `verification.claim_blocker` checks
contract and prior-attempt eligibility; caller policy can add contract adequacy.
`task_claims.release_claim` is for setup failure before real work starts. It
checks claim status/owner and can reject a newer job via `expected_job_id`;
the caller must establish that work never started. `complete_todo_without_verification` is only for an
unclaimed todo task with no running job and no contract; contracted work must
close through acceptance. The caller supplies authoritative closure checks.

## Contracts and verification

Agree a contract **before** delegating: repo, observable result, criteria,
required check commands, allowed effects, risk and whether review is required.
`open_task_contract` checks presence/nonblank fields; semantic adequacy belongs
to the caller. Review defaults on; high-risk contracts cannot waive it.

Create the contract above before launching the writer. This helper records
evidence against that existing contract for a **finished writer job** in the toy repo from
[the bridge guide](code-bridge.md#claude-writes-codex-reviews). Supply the actual
contract id, writer id and independent review findings; call only after confirming that the
review ran over the nonempty writer diff. The helper executes the boundary
check itself and raises if it fails. It does not infer acceptance from prose.

```python
import subprocess
import sys
from lazytools.projects import verification

CHECK = "python -m unittest discover -v"
def record_checked_attempt(store, contract_id, repo, writer_job_id, review_findings):
    assert verification.claim_verification(store, contract_id=contract_id,
                                           job_id=writer_job_id) is not None
    assert verification.start_running(store, writer_job_id) is not None
    check = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"],
                           cwd=repo, capture_output=True, text=True, check=True)
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--stat"],
                          capture_output=True, text=True, check=True)
    assert diff.stdout.strip(), "The uncommitted fixture diff must be nonempty"
    verification.record_checks(store, writer_job_id, checks=[verification.CheckResult(
        command=CHECK, exit_code=check.returncode, output_tail=check.stdout + check.stderr,
    )], diff_summary=diff.stdout)
    verification.record_review(store, writer_job_id, reviewer="codex",
                               findings=review_findings, performed=True, empty_scope=False)
    return verification.accept(store, writer_job_id, reviewer="operator",
                               reason="Inspected diff, independent review and four boundary checks")
```

`accept` refuses missing/failed checks, incomplete required-command coverage,
unperformed review, empty review scope and stale review after reopening. A
promised full suite cannot silently become a narrowed check. `authorization_check`
is an injectable hook; engine independence and caller identity are policy
wiring, not universal enforcement by these generic records. Review findings
need an explicit decision: `request_rework` or `block` instead of accepting
known defects. `retry_review`, `retry_harness`, `reopen_for_empty_review` retain
an audit trail and have specific eligibility rules.
Acceptance records do not automatically close board tasks: caller policy
performs `mark_done`/progress bookkeeping after acceptance.

## Brake and quota telemetry

The brake is on by default per project. `brake.set_project_brake_enabled`
changes that project's flag. `admission.project_admit` reads it before
per-engine admission; disabled means `project_brake_disabled` without a quota
check. Enabled admission checks fresh telemetry, configured absolute ceilings,
autonomous boundary and forecast, with reservations for in-flight jobs.
`EngineBudget` carries caller configuration. Admission refuses new work; it
does not kill active jobs or gate a session's direct file work.

`quota_telemetry.read_quota` / `read_quota_sync` return structured percentages,
window durations, resets, source, observation time and errors. Codex uses
App Server quota; Claude uses its usage report including the five-hour session
window. Unknown telemetry is not spare capacity. This sample uses synthetic
telemetry and starts no engine:

```python
from datetime import UTC, datetime
from lazybridge import Store
from lazytools.projects.admission import EngineBudget, TelemetryReading, WindowReading, project_admit

moment = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
reading = TelemetryReading(engine="codex", source="synthetic demo", observed_at=moment,
                           windows=(WindowReading("codex/10080m", 20, 10080),))
decision = project_admit(Store(), "deployment", budget=EngineBudget("codex"),
                         reading=reading, now=moment)
print(decision.allowed, decision.reason)
```

[Quota routing](code-bridge.md#choose-a-model-from-quota) selects the first
eligible rung of `basic`, `writing`, `thinking`, `critical`. Python `route()`
is pure; `recommend()` adds bounded live quota reads and budgets (or accepts
supplied readings). Both default to autonomous admission.
Bridge `route`/`run --tier` are operator-directed, without project attribution:
forecast/boundary warnings do not block them, but ceilings and telemetry checks
do. Project autonomy admission must be wired by the caller. **Benchmark in
progress**; no quality or quota-efficiency numbers are established.

## Notes, jobs and cost report

`notes.add_project_note` / `recent_project_notes` keep durable handoff context.
`write_project_note` gives checked create-only CAS and optional `on_note` /
diagnostic hooks; duplicates do not fire the hook twice.
`cost_report.project_jobs` and `project_cost_report` report attributed job
records, UTC-today / rolling-seven-day measured spend and unmeasured counts.
`build_fleet_cost_report` aggregates explicit stores; paths are opened read-only,
missing files are not created and unavailable stores are reported.
Subscription turns without measured cost do not imply zero cost. The bridge
currently has no project attribution: callers must connect its job evidence
to project contracts; it does not automatically populate project cost totals.

## MCP configuration and concurrency

Use the [collaboration server setup](mcp-server.md#collaboration-setup).
Store resolution is explicit `store_db` / config `projects_store_db`, then
`LAZYTOOLS_PROJECTS_STORE_DB`, then `LAZYCEO_CEO_STORE_DB`, else in-memory.
A bare server restart therefore loses unpersisted project state.
Core profile emits the common schedule, registry, note, job, cost, quota and
plan tools. `LAZYTOOLS_PROJECTS_TOOLS=full` or `ProjectsTools(profile="full")`
adds contract/verification decisions. Writes require `--allow-unsafe` or
`allow_write=True`.

Records and board edits use compare-and-swap; models round-trip unknown fields.
Owner/brake keys remain separate. This prevents common lost updates, but does
not supply an ACL or a deployment guarantee. Project mechanisms are usable
independently; LazyCEO or another orchestrator owns its autonomy, messaging,
specialist lifecycle and final policy wiring. Advanced hooks include
`review_and_promote_project_plan`, `records.close_project`, caller-owned
operator-directed admission and fleet cost reducers.
