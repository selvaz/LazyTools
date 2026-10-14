"""Default Store key prefixes for the project layer.

Every default here matches exactly what LazyCEO writes today (verified
against ``lazyceo.projects``, ``.plan_edit``, ``.verification``, ``.admission``
and ``.simple.agent`` on branch ``feat/adopt-lazybridge-ext``), so that running
this package against the SAME Store a live CEO process uses reads and writes
the records it already has -- no migration, no second copy.

Every function elsewhere in ``lazytools.projects`` that touches the Store
accepts the matching prefix as a keyword argument defaulting to one of these
constants, rather than hard-coding it, so a deployment that genuinely needs a
different layout (a second, unrelated project registry in the same Store, a
test fixture) can override it without forking the module.
"""

from __future__ import annotations

#: lazyceo.projects.PROJECT_PREFIX -- "ceo:project:<project_id>" -> ProjectRecord.
PROJECT_PREFIX = "ceo:project:"

#: lazyceo.projects.PROJECT_NOTE_PREFIX -- "ceo:project-note:<project_id>:<uuid>" -> note record.
PROJECT_NOTE_PREFIX = "ceo:project-note:"

#: NEW, not written by any installed LazyCEO today -- see docs/projects.md's
#: "owner model". Deliberately a SEPARATE key per project_id, never a field
#: inside the ``ProjectRecord`` blob itself: LazyCEO's own pydantic model
#: does not know this field, and its CAS mutations (``_apply``) round-trip
#: the record through that model, which silently drops unknown fields on
#: write (see ``lazyceo.projects``'s own "Rollout hazard" comment). A
#: separate key cannot be dropped that way.
PROJECT_OWNER_PREFIX = "ceo:project-owner:"

#: NEW, same reasoning as PROJECT_OWNER_PREFIX -- the per-project quota-brake
#: on/off switch, kept out of the main record for the same CAS-safety reason.
PROJECT_BRAKE_PREFIX = "ceo:project-brake:"

#: lazybridge.ext.planners.durable_blackboard.DurableBlackboard's own default
#: key_prefix. A project's board is DurableBlackboard(store, plan_id=f"project:{project_id}")
#: with this prefix, i.e. key f"blackboard:project:{project_id}".
BOARD_KEY_PREFIX = "blackboard:"

#: lazyceo.verification.TASK_CONTRACT_PREFIX -- "ceo:task-contract:<contract_id>" -> TaskContract.
TASK_CONTRACT_PREFIX = "ceo:task-contract:"

#: lazyceo.verification.VERIFICATION_PREFIX -- "ceo:verification:<job_id>" -> Verification.
VERIFICATION_PREFIX = "ceo:verification:"

#: lazyceo.admission.ADMISSION_PREFIX -- "ceo:admission:<engine>" -> reservations + decision trail.
ADMISSION_PREFIX = "ceo:admission:"

#: lazyceo.simple.agent.JOB_PREFIX -- "ceo:codex-job:<job_id>" -> delegated-job record
#: (plan_id, task_index, status, cost_usd, created_at, finished_at, kind, cost_unknown, ...).
#: This package never WRITES job records (that stays LazyCEO's/the code-bridge's job),
#: only reads them for project-scoped cost/job-status reporting.
JOB_PREFIX = "ceo:codex-job:"

#: The CEO's own productive-loop Store, hard-coded in lazyceo.fleet_processes.CEO_STORE_DB
#: and passed as --store-db to the main agent process. Documented default only -- every
#: test and the MCP provider itself always resolve an explicit path (env var or config),
#: never silently fall back to touching a real deployment's file.
DEFAULT_CEO_STORE_DB = r"C:\ProgramData\lazyceo\ceo_simple.sqlite"

#: lazyceo.simple.agent.CEO_STORE_DB_ENV -- the env var LazyCEO already uses to pass its
#: own store path down to child specialist processes. Reused here (not reinvented) as the
#: env var the MCP provider checks first.
CEO_STORE_DB_ENV = "LAZYCEO_CEO_STORE_DB"
