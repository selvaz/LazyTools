# The project layer: `lazytools.projects`

Project management used to live only inside LazyCEO. This package is the
generic MECHANISM half of that: a durable project registry, a plan-editing
layer over LazyBridge's `DurableBlackboard`, a task acceptance-contract /
verification state machine, and an engine quota brake — exposed over
LazyTools' MCP server so a Claude Code session can do most of what the CEO
does to its own projects. LazyCEO keeps the POLICY: per-project autonomy
levels, Telegram, specialist lifecycle, brake ceiling *numbers*, contract
*wording*, ceo-specific prompts.

LazyCEO adopting this package (importing `lazytools.projects` and deleting
its own copies of `projects.py`/`project_schedule.py`/`project_timeline.py`/
`plan_edit.py`/`task_claims.py`/`verification.py`/`quota.py`/`admission.py`)
is a separate, later step, not done here. Everything below describes the
package as it stands today, usable independently of that migration.

## Layering

```
lazybridge                     generic primitives: Store, DurableBlackboard,
                                ext.approval, ext.delegation,
                                engines.codex.usage / engines.claude_code.usage
        |
lazytools.projects              mechanism: registry, schedule, plan editing,
                                contracts, verification, quota brake
        |
lazytools.connectors.projects   MCP tool surface (ProjectsTools)
        |
lazyceo (today)                 ITS OWN copies of the same mechanism, plus
                                 POLICY: autonomy levels, Telegram, specialist
                                 lifecycle, brake numbers, contract wording
```

Once LazyCEO adopts this package, the middle layer disappears and LazyCEO's
own code becomes purely POLICY wired on top of `lazytools.projects` and
`lazytools.connectors.projects`.

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
| `contracts.py` | `lazyceo.verification` (the `TaskContract` half) | nothing of substance — contract-open now does presence/non-blank checks only, not LazyCEO's `deterministic_contract_findings` wording |
| `verification.py` | `lazyceo.verification` (the `Verification` state machine) | `_refuse_if_not_the_ceos_to_accept` → `accept`'s `authorization_check` hook; `_contract_inadequate_for` → `claim_blocker`'s `contract_adequate` hook |
| `intake.py` | `lazyceo.project_intake` + a thin `promote_project_plan` | the LLM falsifiability reviewer call (`contract_review.review_contract_falsifiability`) — the calling agent plays that role itself |
| `quota_telemetry.py` | `lazyceo.quota` | nothing — already pure mechanism over LazyBridge's own usage readers |
| `admission.py` | `lazyceo.admission` | `_human_approval_exists`/`_consume_human_approval`/`restore_human_approval` (Telegram approval tickets) → `operator_directed: bool`, resolved by the caller; **new**: `project_admit`, the per-project brake switch |
| `cost_report.py` | `lazyceo.cost_report` (helpers only) | the fleet-wide, cross-specialist-store aggregation (`build_fleet_cost_report`) — needs the specialist registry, out of scope; narrowed to one project's own job records |
| *(not ported)* | `lazyceo.fleet_report` | entirely specialist-lifecycle/process telemetry — out of scope |
| *(not ported)* | `lazyceo.project_work` | `stop_project_work`/`resume_project_work`'s specialist-stopping — out of scope; `pause_project`/`resume_project` in `records.py` are pure record bookkeeping only |

## The owner model

Every project has an owner: `"ceo"`, `"claude"`, or `"shared"`. Stored at its
own key (`ceo:project-owner:<project_id>`), **never** as a field inside the
`ProjectRecord` blob — see "Concurrency" below for why. A project with no
owner record at all — every project any currently installed LazyCEO has ever
created — reads as `"ceo"`.

- `owner.get_project_owner(store, project_id)` / `set_project_owner(...)`.
- Changing owner is one write, no data copy: it never touches the project
  record or its board.
- `records.list_projects(..., owner=...)` and `timeline.render_project_timeline(..., owner=...)`
  filter by it.
- The MCP provider's `owner` parameter additionally accepts `"default"`
  (Claude Code's own view: `claude` + `shared`, excluding `ceo`) and `"all"`.

**Owner is a label, not an enforced ACL — today.** Nothing in this package
stops a caller from mutating a `ceo`-owned project's plan, and nothing in
LazyCEO (yet) skips a `claude`-owned project. Making the CEO ignore `claude`
projects is explicitly a *later* LazyCEO change; this package only makes
that change easy (one `owner` field to filter on), not automatic.

## The brake rule

The quota brake (`admission.py`) applies to **project work** — a delegated
job attributed to a project, whoever launches it — never to a Claude Code
session's own direct work (reading files, answering a question, editing code
in its own turn). A caller doing direct work simply never calls
`project_admit`/`admit` at all; there is no "off" switch for direct work
because there is no gate on it in the first place.

For project work, the brake is **on by default**, per project, switchable
off:

```python
from lazytools.projects import brake

brake.get_project_brake_enabled(store, "my-project")        # True by default
brake.set_project_brake_enabled(store, "my-project", False)  # opt out
```

`admission.project_admit(store, project_id, budget=..., reading=...)` checks
the project's own switch first; if disabled, it returns an allowed decision
with `reason="project_brake_disabled"` and never consults the engine's
quota at all. If enabled, it defers to `admit()` — the per-engine
ceiling/boundary/forecast brake, unchanged in spirit from LazyCEO's. Ceiling
and boundary numbers (`EngineBudget`) stay fleet-wide **configuration**,
never touched by the per-project switch.

## Concurrency: what is safe with a live CEO process

The live CEO process writes the *same* Store (when pointed at it — see
"Store resolution" below), under its own, currently-deployed pydantic models
for `ProjectRecord`/`TaskContract`/`Verification`. Two hazards, and how each
is handled:

1. **A newer LazyCEO model drops an older caller's unknown fields on
   write.** LazyCEO's own `projects.py` documents this about itself (an old
   model silently drops a newer record's fields on a read-modify-write). The
   same hazard runs the OTHER way today: LazyCEO's *current* `ProjectRecord`
   has two fields (`autonomy_level`, `paused_specialists`) that
   `lazytools.projects.records.ProjectRecord` deliberately does not know
   (they are CEO policy, out of scope here). If this package's model
   silently dropped them on every write, any of its mutators
   (`pause_project`, `touch_project_progress`, …) would quietly erase a
   live CEO project's autonomy level or paused-specialist list the next
   time it ran. **Fixed by `model_config = ConfigDict(extra="allow")`** on
   every record type here (`ProjectRecord`, `TaskContract`, `Verification`,
   `CheckResult`, `ReviewRecord`): an unknown field round-trips through
   `model_validate` → `model_copy(update=...)` → `model_dump(mode="json")`
   unchanged. This is verified by
   `test_projects_records.py::test_apply_round_trips_unknown_fields_extra_allow`.
   The reverse direction — LazyCEO's own (narrower, `extra` defaulting to
   "ignore") model reading a record this package wrote — is safe simply
   because this package never adds a *new* field to any of these three
   record shapes; it only omits two CEO-only ones.

2. **Owner and the brake switch must never be lost to a CEO write.** Both
   are stored at their OWN keys (`ceo:project-owner:<id>`,
   `ceo:project-brake:<id>`), never inside the `ProjectRecord` blob. LazyCEO's
   own mutators never touch those keys (they do not know they exist), so
   there is nothing for them to race or clobber.

**Which write tools are safe to use while a live CEO process is also
writing the same Store, and why:**

| Write tool / function | Safe concurrently? | Why |
|---|---|---|
| `projects_set_owner`, `projects_set_brake_enabled` | **Yes** | separate keys, never touched by the CEO |
| `projects_create` (`records.open_project`) | **Yes** | create-only CAS against a key the CEO has not written yet (a fresh `project_id`) |
| `projects_pause`/`resume`/`close`, `projects_set_deadline`, `projects_add_note`, `projects_review_plan`/`promote` | **Yes, with the caveat above** | all go through `records._apply`'s CAS, which round-trips unknown fields (fix #1) |
| `projects_retire_task`, `projects_reopen_task`, `projects_reopen_done_task`, `projects_revise_plan`, `projects_schedule_task` | **Yes** | whole-document CAS on the board (`blackboard:project:<id>`), same mechanism `DurableBlackboard` itself uses; the task dicts in LazyCEO's board carry no extra fields this package does not already preserve (it reads/writes the same dict shape, not a narrower pydantic model) |
| `projects_open_contract`, `projects_accept_verification`, `projects_request_rework`, `projects_block_verification`, `projects_retry_review`, `projects_retry_harness`, `projects_reopen_for_empty_review` | **Yes** | `TaskContract`/`Verification` also `extra="allow"`, same reasoning as fix #1; CAS-guarded by current status, exactly like LazyCEO's own equivalents |

**Not safe, and deliberately not exposed:** nothing in this package's write
surface mutates `autonomy_level`, `paused_specialists`, Telegram state,
specialist process lifecycle, or runs a merge/release — those stay
exclusively LazyCEO's, so there is no concurrent-write hazard with them to
even discuss.

**One thing this package cannot protect against:** if a *future* LazyCEO
schema adds a third field to `ProjectRecord`/`TaskContract`/`Verification`,
this package's `extra="allow"` models will keep round-tripping it correctly
— but this package will not *validate* or *interpret* it (by design: that
field belongs to a newer LazyCEO than this port). When LazyCEO imports this
package instead of keeping its own copy (the later step), this entire
two-model hazard disappears, because there will only be one model again.

## Store resolution

`lazytools.connectors.projects.tools.ProjectsTools` resolves its `Store`
path as: explicit `store_db` (or `data_source["projects_store_db"]` through
the MCP provider) → `LAZYTOOLS_PROJECTS_STORE_DB` env var →
`LAZYCEO_CEO_STORE_DB` env var (the one LazyCEO itself already sets for a
specialist child process) → **in-memory** if none of those are set.

It deliberately does **not** fall back to LazyCEO's documented production
path (`keys.DEFAULT_CEO_STORE_DB`,
`C:\ProgramData\lazyceo\ceo_simple.sqlite`) on its own. `Store.__init__` runs
schema DDL immediately — merely *constructing* a provider with no
configuration at all must never open a real connection against a live
deployment's file. `DEFAULT_CEO_STORE_DB` documents the value a real
deployment sets one of the two env vars **to**, to see the live CEO's
projects; it is not a silent default this code chooses for itself. (Same
convention the existing `pulse` connector already uses for the same kind of
shared CEO state.)

Tests in this package always pass an explicit path (a temp file, or
in-memory `Store()` directly). The one place a REAL production file is ever
touched is a read-only `sqlite3` backup copy, made once, by hand, for the
smoke test below — never through this package's own default resolution.

## What LazyCEO must change to adopt this

1. Replace `from lazyceo.projects import ...` (and the seven sibling
   modules) with `from lazytools.projects import ...` at each call site;
   delete `lazyceo/{projects,project_schedule,project_timeline,plan_edit,
   verification,quota,admission}.py` (keep `task_claims.py`'s
   `close_accepted_task` — it stays CEO-specific — or migrate it to call
   `verification.accept(..., authorization_check=lazyceo's own check)`
   directly).
2. Add `autonomy_level`/`paused_specialists` back as LazyCEO-local fields —
   either keep them as separate keys (this package's `owner`/`brake`
   pattern) or accept that `lazytools.projects.records.ProjectRecord`'s
   `extra="allow"` already protects them inside the shared blob; either
   choice is adoptable today without a migration.
2. Wire `verification.accept`'s `authorization_check` to
   `lazyceo.boost.effective_capability`/`lazyceo.project_autonomy` (this is
   exactly `_refuse_if_not_the_ceos_to_accept`, unchanged in substance).
3. Wire `verification.claim_blocker`'s `contract_adequate` to
   `lazyceo.simple.agent._contract_inadequate_for`.
4. Set `owner="ceo"` is already the default for every unmigrated project —
   no backfill needed. LazyCEO starts filtering its own worklist by
   `owner != "claude"` whenever it is ready to.
5. Keep running its own `lazyceo.project_work.stop_project_work`/
   `resume_project_work` (specialist-stopping) on top of this package's
   `records.pause_project`/`resume_project` — those two stay thin record
   bookkeeping here on purpose.
6. Point the `projects` MCP provider's `projects_store_db` (or
   `LAZYTOOLS_PROJECTS_STORE_DB`) at the exact same file LazyCEO's own
   `--store-db` already uses, so Claude Code sessions see the identical
   registry.

## Known gaps carried over from LazyCEO, not fixed here

One Codex review (`codex_review_changes`, scope=branch vs origin/main) found six
issues. Three were in this package's OWN new code and are fixed (see the
commit history): `verification.accept` now fences its final write against
the exact snapshot it validated, not whatever `_transition` re-reads later;
`projects_brake_status` no longer blocks on telemetry when a project's brake
is already disabled, and now agrees with shadow mode (`preflight`, not a raw
`decide`). Three more are **inherited unchanged from LazyCEO's own
equivalents** — not introduced by this port, and deliberately not changed
here to keep "same behavior" true to the letter:

- `verification.accept` checks that every *recorded* check passed, but never
  checks that `current.checks` actually covers every command in
  `contract.required_checks`. A contract naming two required checks whose
  attempt only ever ran (and passed) one of them is accepted. LazyCEO's own
  `accept_verification` has the identical gap.
- `intake.promote_project_plan` only installs its plan onto the board when
  the board is still empty (`if not board.snapshot().tasks: board.set_plan(...)`).
  If an earlier, interrupted promotion already left a *different* plan on
  the board, promoting a newly-reviewed plan silently keeps the stale one
  while reporting the new one as installed. LazyCEO's `promote_project` tool
  has this exact shape.
- `quota_telemetry._read_claude` reads only `snapshot.weekly`, never a
  session/five-hour window if the underlying `fetch_claude_usage()` exposes
  one. Admission could then admit work that is fine on the weekly window but
  already exhausted on a shorter one. LazyCEO's `lazyceo.quota._read_claude`
  reads the identical field.

Each is a real finding and a candidate for a follow-up PR (in whichever
codebase ends up owning this mechanism after LazyCEO's adoption) — flagged
here rather than silently carried forward.

## Not exposed, by design

Delegation (execution of a task) — a Claude Code session uses
`lazytools-code-bridge` for that, not this provider. Telegram. Specialist
lifecycle (`lazyceo.specialists`/`specialist_lifecycle`). Autonomy-LEVEL
changes (`set_project_autonomy`). Git merges/releases. None of these are
partially exposed either — there is no read-only peek at specialist
processes or autonomy levels through this provider; that visibility, if
ever wanted, is LazyCEO's own to add.
