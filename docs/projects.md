# Projects: shared state for collaborative work

`lazytools.projects` connects Claude Code and Codex work through a LazyBridge
Store: project records, plans on `DurableBlackboard`, schedule dates, claims,
contracts, verification, notes, cost reports and quota admission. The same
mechanisms are exposed through `ProjectsTools` and the `projects` MCP provider.
LazyTools 0.10.3 is Alpha. Caller policy chooses who may accept work and what
the checks must prove; project bookkeeping does not merge or release code.

## Layering

LazyBridge supplies generic primitives: `Store`, `DurableBlackboard`, approval
queues, delegation and native usage readers. `lazytools.projects` supplies
project mechanisms on those primitives. `lazytools.connectors.projects`
exposes them through `ProjectsTools`; LazyCEO or another orchestrator supplies
autonomy policy, messaging, specialist lifecycle and acceptance authority.

```text
lazybridge                    Store, DurableBlackboard, engines, approval
    |
lazytools.projects            registry, plan, schedule, claims, verification, admission
    |                    \
ProjectsTools / MCP           LazyCEO or another caller's policy adapters
```

The project layer imports no LazyCEO runtime. Key prefixes retain the shared
record layout, so an explicitly configured common Store can expose existing
projects without copying them. Adopting the package removes duplicate mechanism
implementations in a caller; it does not remove the generic primitives or MCP
surface. This documentation change does not update or deploy a running CEO.

## Module map (what moved from which LazyCEO file)

| `lazytools.projects` module | ported from | left behind (stays CEO policy) |
|---|---|---|
| `records.py` | `lazyceo.projects` | `autonomy_level`, `paused_specialists` fields; `set_project_autonomy`; `add_operator_note`'s Telegram wake |
| `owner.py` | — (new) | — |
| `brake.py` | — (new) | — |
| `notes.py` | `lazyceo.projects` (`add_project_note`, `recent_project_notes`, `project_board_summary`) | `add_operator_note`'s CEO-wake/Telegram prefix |
| `schedule.py` | `lazyceo.project_schedule` | nothing — already pure mechanism |
| `timeline.py` | `lazyceo.project_timeline` | nothing — `blocked_reasons` stays caller-supplied |
| `plan_edit.py` | `lazyceo.plan_edit` | nothing — already pure mechanism |
| `task_claims.py` | `lazyceo.task_claims` | `close_accepted_task` (reaches LazyCEO's boost/autonomy; see `verification.accept`'s hook instead) |
| `contracts.py` | `lazyceo.verification` (the `TaskContract` half) | nothing of substance — contract-open does presence/non-blank checks, not LazyCEO's `deterministic_contract_findings` wording |
| `verification.py` | `lazyceo.verification` (the `Verification` state machine) | `_refuse_if_not_the_ceos_to_accept` → `accept`'s `authorization_check` hook; `_contract_inadequate_for` → `claim_blocker`'s `contract_adequate` hook |
| `intake.py` | `lazyceo.project_intake` + a thin `promote_project_plan` | the LLM falsifiability reviewer call (`contract_review.review_contract_falsifiability`) — the calling agent plays that role itself |
| `quota_telemetry.py` | `lazyceo.quota` | nothing — already pure mechanism over LazyBridge's own usage readers |
| `admission.py` | `lazyceo.admission` | `_human_approval_exists`/`_consume_human_approval`/`restore_human_approval` (Telegram approval tickets) → `operator_directed: bool`, resolved by the caller; **new**: `project_admit`, the per-project brake switch |
| `cost_report.py` | `lazyceo.cost_report` | specialist enumeration and fresh-connection policy; callers provide stores or read-only paths |
| *(not ported)* | `lazyceo.fleet_report` | entirely specialist-lifecycle/process telemetry — out of scope |
| *(not ported)* | `lazyceo.project_work` | `stop_project_work`/`resume_project_work`'s specialist-stopping — out of scope; `pause_project`/`resume_project` in `records.py` are pure record bookkeeping only |

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

### The owner model

Every project resolves to `ceo`, `claude` or `shared`. The owner lives at
`ceo:project-owner:<project_id>`, outside the `ProjectRecord` blob; missing
owner metadata defaults to `ceo`. `owner.get_project_owner` /
`set_project_owner` read/write that label without copying the project or board.
`records.list_projects(..., owner=...)` and
`timeline.render_project_timeline(..., owner=...)` filter by it.

MCP list/timeline default to `owner="default"`, meaning `claude` + `shared`;
`all` includes every owner and a named owner selects one. Gantt's MCP default
is `all`, so its view is intentionally broader than the default project list.
Single-record access is not restricted by these list filters.

**Owner is a coordination label, not an ACL.** A caller can mutate a CEO-owned
record if it has the write capability. LazyTools does not make a live CEO stop
working on Claude-owned projects; the CEO must filter its own worklist.
Do not infer that a label change transfers process control or acceptance rights.

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

## The brake rule and quota telemetry

The brake applies to **delegated project work attributed to a project**, whoever
launches it. It does not apply to a Claude Code session's own direct reading,
answering or edits: that caller does not call `project_admit` for its own turn.
There is no direct-work off switch because there is no direct-work gate.

The brake is on by default per project. `brake.set_project_brake_enabled`
changes that project's flag. `admission.project_admit` reads it before
per-engine admission; disabled means `project_brake_disabled` without a quota
check. Enabled admission checks fresh telemetry, configured absolute ceilings,
autonomous boundary and forecast, with reservations for in-flight jobs.
`EngineBudget` carries caller configuration; engine ceiling/boundary numbers
remain fleet-wide inputs and are not rewritten by the per-project switch. Admission refuses new work; it
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

## Tool profiles

Every advertised MCP tool adds context to a host session. `ProjectsTools`
therefore defaults to `core`, or the profile selected by
`LAZYTOOLS_PROJECTS_TOOLS`; explicit `profile=` wins. Invalid profile names
raise `ValueError`. `full` adds contract/verification and advanced operations.
Writes require `allow_write=True` programmatically or the server's
`--allow-unsafe` selection in either profile.

| Profile | Read-only menu | Write-enabled menu | Included work |
| --- | --- | --- | --- |
| `core` | 10 tools | 18 tools | Registry, schedule/timeline/Gantt, board summary, quota/brake status, cost/jobs; create, review/promote, owner/deadline/notes/plan/scheduling writes |
| `full` | 16 tools | 38 tools | Core plus contracts, verification history/decisions, brake writes and additional lifecycle operations |

These counts come from the current `as_tools()` output, including the added
Gantt tool; the old “all 37” count was stale. Exposing `full` does not delegate
a task or stop a specialist. Use the [collaboration server setup](mcp-server.md#collaboration-setup)
and discover the emitted menu for the chosen configuration.

## Store resolution

`ProjectsTools` resolves explicit `store_db` (MCP data-source
`projects_store_db`) first, then `LAZYTOOLS_PROJECTS_STORE_DB`, then
`LAZYCEO_CEO_STORE_DB`, otherwise **in-memory**. An injected Store in the
mechanism functions is already the caller's choice. A bare MCP restart loses
its unpersisted project state.

There is no automatic fallback to `keys.DEFAULT_CEO_STORE_DB` or a live
production path. Constructing `Store` immediately performs schema setup, so
an unconfigured provider must not silently connect to a deployed CEO file.
To share live state, configure exactly the same file the CEO uses and assess
the concurrent-write behavior below. Default key prefixes are shared; paths
must still agree. A separate file with identical keys is a separate registry.
Tests use explicit temporary files or in-memory Stores, never a deployment file.

## Concurrency with a live CEO process

Compare-and-swap (CAS) protects individual records and whole board documents
against lost updates. It does not serialize two complete multi-record workflows
or control a specialist process. A live CEO and an MCP caller can share the
same Store only with those distinctions understood.

Two specific compatibility hazards explain the layout:

1. **Unknown fields on shared records.** `ProjectRecord`, `TaskContract`,
   `Verification`, `CheckResult` and `ReviewRecord` use `extra="allow"`.
   Validation/copy/dump round-trips CEO-local fields such as `autonomy_level`
   and `paused_specialists`, instead of silently dropping them in a mutation.
   This preserves fields; it does not validate or interpret newer caller policy.
   A different writer using a narrower model can still drop fields it does not
   know. Both processes must use compatible shapes and key prefixes.
2. **Owner/brake metadata.** They have separate keys
   (`ceo:project-owner:<id>`, `ceo:project-brake:<id>`), so writing the project
   blob cannot erase them. Two writers of the same metadata key can still
   disagree; a separate key is not an ACL or a multi-key transaction.

| Mutation family | Record-integrity protection | Remaining coordination requirement |
| --- | --- | --- |
| Owner/brake | Separate metadata keys | Agree who may change the setting; CEO worklist must consult it |
| Create project | Create-only CAS on a fresh id | Existing id refuses rather than replaces another project |
| Pause/resume/close, deadline, note/intake fields | Record CAS; unknown fields preserved | Record status does not stop/resume processes |
| Revise/retire/reopen/schedule board tasks | Whole-board CAS preserving existing task dictionaries | Re-read on conflict; started work is not replaceable as untouched work |
| Contract/verification decisions | Status/evidence-fenced CAS; extras preserved | Acceptance authority and semantic adequacy remain caller hooks |
| Project plus board updates | Guards on each key | No transaction across keys; retry from fresh state |

The original “safe concurrently” table described these integrity guarantees,
not permission to race an operator's workflow. No tool here changes Telegram,
specialist lifecycle, autonomy levels, merges or releases. Those operations
remain the orchestrator's responsibility.

## LazyCEO adoption: current responsibilities

The mechanism and policy split remains current. The inspected LazyCEO checkout
still has local `projects.py`/`verification.py` implementations; this LazyTools
release does not promise that every installed CEO has migrated. A local adoption
branch or deployed process must be checked independently. The earlier checklist
is retained as integration work, not a request to delete files blindly:

1. Replace mechanism call sites with this package where signatures and hooks
   match; remove duplicate implementations only after parity verification.
2. Preserve CEO-only fields using `extra_fields` / `record_factory` at creation
   and the shared models' extra-field round-trip, or keep policy in separate keys.
3. Wire `verification.accept(authorization_check=...)` to the CEO's actual
   autonomy/boost authority and `claim_blocker(contract_adequate=...)` to its
   contract wording/adequacy policy.
4. Filter the worklist by owner when desired. Missing metadata defaults to
   `ceo`, so no backfill is needed; list defaults in MCP do not filter CEO jobs.
5. Keep process stop/resume behavior above record pause/resume. Close accepted
   board slots and touch progress only after caller-authorized acceptance.
6. Supply the same Store path and key prefixes, current budget configuration,
   specialist-store enumeration and messaging/approval adapters.

Phase A's intake, closure, notes, admission and fleet-cost hooks are shipped.
There is no need for a new `close_project_checked` or
`admit_with_operator_fallback` API: use the current hooks and operator-directed
admission. This closes the former mechanism-adapter gaps; caller policy wiring
and rollout checks remain. No LazyCEO source is changed by this refresh.

## Known gaps and boundaries

- Owner is not an ACL; the package cannot enforce a caller's acceptance identity
  or make a deployed CEO respect ownership without policy wiring.
- A project/board transition spans separate keys. CAS and rechecks fence stale
  evidence but do not make that transition a multi-key transaction.
- The project layer does not isolate repository checkouts or launch/cancel jobs.
  The bridge locks one repository at a time; it does not create a worktree per
  project attempt. Other writers need their own isolation/locking policy.
- Bridge jobs have no automatic project attribution or cost aggregation.
  Caller policy connects job evidence, claims and contracts.
- Quota is bounded telemetry, not a guaranteed remaining-token budget; unknown
  readings are not spare capacity. Admission gates new work, not active jobs.
- Generic contract validation checks nonblank structure, not falsifiability or
  mandate compliance. A reviewer that did not run remains a failure.
- Hooks run in-process, not through durable delivery. Failure after a winning
  CAS does not undo that write; reconcile records before retrying side effects.

## Installing a reviewed plan

`intake.install_project_plan(store, project_id, *, objective, tasks, current_digest=None) -> str | None`
uses whole-board CAS. Identical reasoning and task text are a no-op. A different
plan replaces an empty or untouched board. Claims, done/failed statuses,
previous attempts (including tasks returned to todo), owners and recorded work
prevent replacement; refusals name the started tasks. Promotion uses this helper
and remains draft on refusal. Existing board keys and task shapes are unchanged.

`intake.promote_project(store, project_id, *, current_digest) -> str | None`
provides the digest-only transition: callers install intake fields and the plan
first. It checks the review and fences the status CAS against that exact record.
An already-open project returns `None` before the digest is looked at, exactly
as LazyCEO's original did (retries and polls of an open project rely on it);
paused/done records refuse.
`promote_project_plan` also verifies that an open project's board matches before
returning idempotent success. `install_project_plan` can check the current review
immediately before each board CAS with `current_digest`.

`async intake.review_and_promote_project_plan(store, project_id, *,
observable_result, deadline, subtasks, reviewer, reviewer_kwargs=None) -> str`
runs the checklist before invoking the async reviewer. The callable receives
`observable_result`, flattened `acceptance_criteria`, `required_checks=[]`,
`allowed_effects` and `store`, plus caller context (such as `root`). It returns
`(findings: list[str], performed: bool)`. Findings or a review that did not run
refuse; the review is recorded only if the project stayed unchanged while
awaiting. The plan is copied before review. Matching open plans skip the reviewer.
The package never selects or calls a model. Hooks propagate exceptions; retries
must re-read state. Project and board keys remain separate, so these guards are
best-effort across keys rather than a multi-key transaction.

`task_claims.complete_todo_without_verification(store, project_id, task_index,
summary, *, board_prefix=BOARD_KEY_PREFIX, job_prefix=JOB_PREFIX,
contract_exists=False, check=None, on_closed=None) -> str` retains the existing
default guards. `check(store, project_id, task_index) -> str | None` runs just
before every CAS attempt; return a refusal string or `None`. This supports a
fresh contract lookup. `on_closed(store, project_id, task_index) -> None` runs
once after a winning close, for example to touch progress. A losing/repeated
close never calls it. Hooks propagate exceptions; an exception after success
does not roll back the close, and this is not a durable delivery guarantee.

`notes.write_project_note(store, project_id, text, *, origin="note", note_id=None,
prefix=PROJECT_NOTE_PREFIX, on_note=None, diagnostic=None) -> NoteWriteResult`
returns `note_id`, `written` (the actual CAS outcome), and `diagnostics`.
`on_note(store, record)` runs once on success and never on a duplicate or lost
race. `diagnostic(message)` receives failed-write diagnostics. The existing
`add_project_note` keeps returning a string id and supports the same optional
hooks. Use the checked API when the caller must distinguish saved from unsaved.
`notes.note_timestamp(value, *, diagnostic=None) -> float` and
`recent_project_notes(..., diagnostic=None)` provide malformed timestamp
diagnostics without changing sorting or legacy timestamp readability.

`admission.admit(store, *, budget, reading, operator_directed=False, review=False,
now=None, prefix=ADMISSION_PREFIX, post_reservation=None) -> AdmissionDecision`
and `project_admit(..., admission_prefix=ADMISSION_PREFIX, post_reservation=None)`
invoke `post_reservation(store, decision, before) -> AdmissionDecision | None`
once after a successful allowed reservation CAS. `before` copies that CAS's
input document, including prior reservations; it never re-reads a later state.
Return `None` to keep the decision or a modified decision with `spent_approval`.
A refused decision (e.g. caller reason `approval_already_spent`) releases the
reservation; hook exceptions release and propagate. Release remains best-effort
under contention. Allowed decisions must keep their reservation identity.
Disabled project brakes and refused admissions skip the hook. Approval provenance,
consumption, exemptions and refund policy belong to the caller. `spent_approval`
is returned in memory only, preserving the existing audit/reservation shapes.

`records.open_project(store, *, project_id, title, objective, size=None,
risk=None, process=None, classification_rationale=None, prefix=PROJECT_PREFIX,
extra_fields=None, record_factory=None) -> ProjectRecord` adds caller fields
in the initial create-only CAS. `extra_fields` is a mapping of additional fields;
it cannot override shared fields. `record_factory(data) -> ProjectRecord` can
validate a caller subclass and add typed defaults (e.g. pass
`CallerProject.model_validate`). It cannot change the already validated shared
fields. Factory/validation errors happen before the CAS and do not consume the id.
`adopt_existing_project(..., **kwargs)` forwards both options. Omitting them
preserves the existing stored shape; shared mutations continue round-tripping extras.

## Not exposed, by design

Delegation (execution of a task) — a Claude Code session uses
`lazytools-code-bridge` for that, not this provider. Telegram. Specialist
lifecycle (`lazyceo.specialists`/`specialist_lifecycle`). Autonomy-level
changes (`set_project_autonomy`). Git merges/releases. There are no dedicated
tools to control these operations or inspect specialist processes. A shared
record can still carry CEO-local extra fields, and `projects_get` returns
preserved extras such as `autonomy_level`; that incidental record visibility
does not make the provider an autonomy-management API.

## Required check coverage

`verification.accept(store, job_id, *, reviewer, reason, expected=None,
prefix=VERIFICATION_PREFIX, authorization_check=None)` now refuses a new
acceptance unless every `contract.required_checks` command has a passed
recorded check. Commands match exactly. An explicitly listed
`allowed_check_exclusions` command can substitute only when removing its
`--deselect`, `-k`, `--ignore` or `--ignore-glob` arguments yields the required
command. Missing commands are listed in `ValueError`. Previously accepted
records are unchanged.

`cost_report.build_fleet_cost_report(store, *, specialist_stores,
job_prefix=JOB_PREFIX, task_prefix=None, primary_name="CEO itself", now=None)
-> dict[str, Any]` sums every job record in every supplied store, across all
projects and unattributed work. `specialist_stores` maps names to `Store`
instances or explicit `Path` values. Paths are opened with SQLite `mode=ro`;
missing files are never created. Optional `task_prefix` includes each agent's
own turn records using `completed_at`, alongside delegated jobs using the first
parseable `finished_at`/`created_at`. Unavailable stores reject all their families
and appear in `unavailable` and `breakdown`. The caller enumerates specialists
and supplies any fresh connection policy.

`cost_totals(records, *, now, timestamp_fields=("finished_at", "created_at"))
-> dict[str, float]` and `unmeasured_cost_counts(records, *, now,
timestamp_fields=("finished_at", "created_at")) -> dict[str, int]` expose the
reducers independently. `read_store_records_read_only(path, *, prefixes)
-> dict[str, list[dict[str, Any]]]` reads all requested families in one SQLite
snapshot. Reports use UTC midnight and a rolling seven-day window, ignore
future/unparseable timestamps, and preserve the original scalar cost rules.
`project_jobs` and `project_cost_report` keep their existing APIs and behavior.

All Phase A entry points and hook/result types are also exported from
`lazytools.projects`. Regression parity fixtures in
`tests/fixtures/projects_parity.json` retain LazyCEO function source at
`70a481c` (promotion, closure, fleet) and the adoption wrapper's source
(admission). Tests compile those functions with injected dependencies, without
requiring LazyCEO or touching its stores. Known deliberate improvements beyond
original parity are A1/A2 and refusing a reviewer that reports `performed=False`
with no findings. `promote_project` keeps the original order (open = success,
digest not consulted); the parity test pins the open-with-a-newer-digest case.
