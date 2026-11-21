# Read-only Repository Maintenance Pipeline — first executable slice

This implements a first, **experimental** slice of the [full public pipeline specification](repo-maintenance-pipeline-spec.md). It runs an actual LazyBridge Plan, not a new custom scheduler:

1. Resolve configured Git roots and pin exact HEAD/base SHA identifiers.
2. Reuse or refresh local RepoMap indexes with SHA/config-keyed snapshot metadata.
3. Compute bounded Git changed-path evidence and renames (or initial tracked-file inventory).
4. Request bounded, explicitly unverified static impact evidence for changed source files.
5. Optionally run nested reviewer Agents as Plan tools (parallel when multiple).
6. Optionally run operator-approved tests on detached temporary Git worktrees.
7. Write SHA-addressed, immutable per-run JSON/Markdown reports. No source changes or automatic PRs.

The deterministic mode requires **no LLM or MCP**. Verification is opt-in and can be granted only via the Python API.

## Install

Install the separately distributed MIT-licensed [SylphxAI/repomap](https://github.com/SylphxAI/repomap) binary, pinned to a tested version:

~~~sh
npm install --global @sylphx/repomap@1.9.0
repomap version
~~~

The Python adapter uses LazyTools' existing LazyBridge dependency. No mandatory Python extras were added.

## Current experimental Python API

~~~python
from pathlib import Path
from lazytools.maintenance import (
    Dependency, MaintenancePipeline, RepoSpec, Workspace,
)

workspace = Workspace(
    repos=(
        RepoSpec("library", Path("../library")),
        RepoSpec("service", Path("../service")),
    ),
    # This must be outside all repository roots.
    state_dir=Path("../maintenance-state"),
    dependencies=(Dependency(consumer="service", provider="library"),),
)
pipeline = MaintenancePipeline(workspace, use_embeddings=False)
first = pipeline.execute()
print(first["report_json"], first["report_markdown"])

# On a later HEAD, compare the library to the recorded first SHA:
later = pipeline.execute(base_refs={"library": first["sha"]["library"]})
print(later["changes"]["library"], later["cross_repo_candidates"])
~~~

Operators must configure every repository root. Worktrees must be clean by default; the code refuses dirty source roots rather than indexing a mixed revision and incorrectly attributing it to HEAD. State/cache/report directories must be outside source worktrees. RepoMap's own engine cache remains external.

An initial scan with no base ref inventories Git-tracked files; it does not pretend they are new commits. If you want a PR diff, prepare a trusted checkout at the relevant head SHA and pass an explicitly known base SHA. A remote branch name does not prove current freshness.

## Nested reviewer agents

The Plan can accept ordinary LazyBridge Agents with distinct names starting with review_. They receive bounded JSON evidence; with multiple reviewers, the Plan uses a parallel band and aggregates their labeled outputs.

~~~python
from lazybridge import Agent, LLMEngine

reviewer = Agent(name="review_architecture", engine=LLMEngine("your-model"))
pipeline = MaintenancePipeline(workspace, reviewers=[reviewer])
result = pipeline.execute()
assert result["reviews"]["status"] == "unverified_suggestions"
~~~

**Security:** the deterministic Plan has no write tools. But externally supplied reviewer agents may themselves carry writing tools, native coding abilities, filesystem permissions, or credentials. They must be explicitly confined by the caller. The first public release must add a reviewer capability gate and sandbox before any unattended deployment.

Review notes are **unverified suggestions**, never test-pass proof or approval to change a repository.

## Evidence and safety

Reports include schema_version 1, exact SHA vector, base SHAs, dirty status, cache hit/miss, indexing time, bounded Git changes, static impact previews, declared cross-repo candidates, warnings and review/verification status. The default read-only scan reports:

- status = review_required
- verified = false
- verification.status = not_run (or passed_selected_checks / failed / incomplete with explicitly approved checks)

A declared dependency means a consumer *might* need a compatibility check, not that static code references were established across repositories. When an impact query fails, it is labeled unverified rather than silently yielding no impact.

## Tests and current limitations

Run the focused tests with:

~~~sh
PYTHONPATH=src python -m pytest -q tests/test_codemap.py tests/test_maintenance.py tests/test_maintenance_cli.py tests/test_maintenance_verification.py
~~~

GitHub CI adds a real RepoMap binary smoke with two successive Git commits, first/second snapshots and a verified cache hit. Ruff, mypy, multiple Python versions and MCP majors run in the regular LazyTools matrix.

**Not yet included:** GitHub event triggers, safe patch/PR generation, full cross-repo behavioral compatibility, durable Plan checkpoints and idempotent locking, arbitrary-language coverage, remote-head freshness checks, and end-to-end coding-agent token/time savings. These are defined as future phases in the [full spec](repo-maintenance-pipeline-spec.md).


## Public TOML configuration and CLI

The experimental CLI works without LazyCEO, LazyStudio, MCP or an LLM. Store the following operator-owned manifest *outside* the source repositories (the state directory must also be outside all worktrees):

~~~toml
schema_version = 1
state_dir = "maintenance-state"
require_clean = true

[mapping]
embeddings = false
timeout_seconds = 45

[[repos]]
id = "library"
root = "../library"

[[repos]]
id = "service"
root = "../service"

[[dependencies]]
consumer = "service"
provider = "library"
import_prefix = "library" # optional dotted Python import prefix
~~~

~~~sh
lazytools-maintenance --config maintenance.toml --validate-only --json
lazytools-maintenance --config maintenance.toml --json
lazytools-maintenance --config maintenance.toml --base library=PREVIOUS_SHA
# Also supported: python -m lazytools.maintenance --config maintenance.toml
~~~

Validating roots and SHAs does not need RepoMap installed. A normal scan requires an explicitly installed RepoMap CLI. The manifest may not grant commands, test execution, source writes or select arbitrary executables; unexpected keys are rejected. Only configured repositories are inspected. Optional import_prefix matching finds bounded static Python import statements in tracked consumer files. It does not guarantee behavioral compatibility or cover relative/dynamic imports.

## Operator-approved tests (opt-in only)

~~~python
import sys
from lazytools.maintenance import CheckSpec, MaintenancePipeline

pipeline = MaintenancePipeline(
    workspace,
    checks=(CheckSpec(
        id="library-tests",
        repo="library",
        argv=(sys.executable, "-m", "pytest", "-q", "tests/unit"),
        timeout_seconds=120,
    ),),
)
result = pipeline.execute()
print(result["verification"]["status"])
# Possible: passed_selected_checks / failed / incomplete
assert result["verified"] is False
~~~

Checks are granted in trusted Python code, not in the TOML, an LLM prompt, a PR or the public CLI. Each command uses an argument vector, not a shell, and runs on a disposable detached worktree at the pinned Git SHA. The report includes bounded stdout/stderr, exit code, duration, truncation flag and the exact SHA. Source checkout files remain untouched; Git briefly registers and removes the detached worktree.

**Security boundary:** a temporary Git worktree is *not* an OS sandbox and does *not* enforce network isolation. Only execute code from trusted revisions, with proper container/CI isolation as appropriate. A restricted environment avoids forwarding ambient API credentials, but the subprocess retains its operating-system permissions. No automatic PR writes, merges or deployment capabilities are provided.

Every scan has its own run_id and distinct report files; a later run cannot overwrite an older report for the same SHA.

**Freshness rule:** even when SHA-keyed snapshot metadata exists, the pipeline runs the RepoMap index command again to revalidate the engine cache. `cache_hit` means *metadata reused*, not indexing skipped. Reports record `refresh_seconds` for every run. Dirty worktrees never reuse SHA-only metadata.

## Runnable two-repository example

Run `PYTHONPATH=src python examples/maintenance_two_repos.py` after installing the optional RepoMap 1.9.0 binary. It creates two disposable local Git repositories, changes a library API, then demonstrates a SHA-pinned consumer import finding. The example never alters a real repository or requires an LLM.


## Experimental self-improvement: existing writer and independent reviewer

The optional local improvement flow **reuses** the coding engines and review tools already in LazyTools; it does not implement a second LLM reviewer:

- Writer: existing CodeWriteTools with claude_code_write **or** codex_write.
- Independent reviewer: existing codex_reviewer / codex_code_review **or** claude_reviewer / claude_code_review, always the **other engine family**.
- Deterministic outer controller: LazyBridge Agent(engine=Plan(...)) with a read-only maintenance scan step followed by an explicitly authorized local-proposal step.
- Approved checks: the same CheckSpec and run_approved_checks machinery; every check starts in its own clean detached worktree.
- For a **published GitHub PR**, continue to use the pre-existing LazyTools CodeBridge Navigator. It already owns exact-head review, repeat/fix limits, P0/P1 escalation, test gates and no-merge policy. Do not introduce a parallel production PR state machine.

This initial implementation creates **only a local patch artifact**, never a GitHub PR or merge. It clones the selected repository into a private temporary Git checkout with no remotes and a distinct Git object store, applies a small change with the existing coding engine, commits that change **only in the private clone**, asks the opposite engine to review the cumulative diff and runs explicitly granted checks. The clone is deleted; a machine-readable report and a cumulative patch remain outside the source repositories.

Example from a trusted Python application:

~~~python
import sys
from lazytools.maintenance import (
    CheckSpec, ImprovementRequest, MaintenanceSelfImprovementPlan,
    MaintenancePipeline,
)

scan = MaintenancePipeline(workspace, use_embeddings=False)

request = ImprovementRequest(
    repo="library",
    objective="Fix the public API compatibility bug and add the existing regression coverage",
    allowed_paths=("library/api.py", "tests/test_api.py"),
    writer_engine="claude",
    reviewer_engine="codex",
    approved_local_proposal=True,  # explicit permission for LOCAL PATCH only
    max_rounds=2,                   # up to 3; critical findings stop immediately
    checks=(
        CheckSpec(
            id="targeted-tests",
            repo="library",
            argv=(sys.executable, "-m", "pytest", "-q", "tests/test_api.py"),
        ),
    ),
)

proposal = MaintenanceSelfImprovementPlan(scan, request).run()
print(proposal["status"])
print(proposal.get("patch_path"))
# A patch is not merged, pushed or submitted as a PR.
~~~

The default approval is False and the operator must enumerate exact allowed changed files. If writer and reviewer select the same engine family, construction fails. The reviewer must return the existing independent-review marker and a strict JSON findings contract (P0/P1/P2/P3 severities); missing, malformed or failed reviews **never** count as clean. P0/P1 escalates; lower-severity findings can trigger bounded additional writer rounds. A clean review without operator-approved tests is marked needs_approved_checks, **not** ready. All successful checks still yield proposal_ready_for_human_review, not automatic acceptance.

**Security/operational limits:** the existing coding agents may have their own runtime access or network capabilities, and running untrusted repository test code is active execution. A private Git clone is not a secure OS sandbox. Use trusted checkouts or isolated runners with network restrictions for unattended usage; the built-in code writer is configured with Claude Bash disabled, and no remote is retained in the proposal clone. Do not give these tools high-privilege credentials. The initial feature is for opt-in developer-controlled experimentation; it does not launch live coding sessions automatically from GitHub events and does not provide automatic PR creation.

The local loop is **not** a second implementation of CodeBridge Navigator: it prepares an auditable patch before any PR exists. Once an operator publishes an approved patch as a PR, use CodeBridge Navigator for the head-bound review/repair cycle. No automatic merge is provided by either path.


## Complete Plan: scan, local improvement, draft PR and existing Navigator

These are optional steps of ONE declared LazyBridge Plan. The independent
reviewer is NOT reimplemented; the PR stage reuses the existing
`lazytools.code_bridge._navigator.Navigator` with its exact-head review,
bounded fixes and test gates. No component performs an automatic merge.

A local patch grant, a draft PR publication grant, and a Navigator execution
grant are **independent**. All are off by default. The handoff verifies the
SHA-256 patch digest, approved paths, original Git SHA, remote base SHA,
independent clean review and approved tests. It creates a unique
`maintenance/<run-id>` branch, never force-pushes or touches main.

~~~python
from lazytools.maintenance import (
    MaintenancePipeline, MaintenanceSelfImprovementPlan, ImprovementRequest,
    PublicationApproval, NavigationApproval, CheckSpec,
)

scan = MaintenancePipeline(workspace, use_embeddings=False)
request = ImprovementRequest(
    repo="library",
    objective="Fix the compatibility regression with a small, tested patch",
    allowed_paths=("library/api.py", "tests/test_api.py"),
    writer_engine="claude", reviewer_engine="codex",
    approved_local_proposal=True,
    checks=(approved_api_check,),  # trusted CheckSpec
)
plan = MaintenanceSelfImprovementPlan(
    scan, request,
    publication=PublicationApproval(
        "OWNER/REPO", base_branch="main", approved_publish_draft=True,
    ),
    navigation=NavigationApproval(
        reviewer_command="trusted-review-wrapper",
        check_commands=("pytest -q tests/test_api.py",),
        approved_navigate=True, max_rounds=2,
    ),
)
result = plan.run()
print(result["status"], result.get("pr_url"))
~~~

This example is **not an instruction to grant writes automatically**.
The reviewer command implements the established
[CodeBridge Navigator contract](code-bridge.md#navigator-r1-the-merge-ready-loop):
it posts the independent-review marker to the PR and prints strict JSON
with a reviewer_model and findings list. It must be supplied by trusted
application code; neither TOML nor an LLM can supply it. Existing
CodeBridge cloud fixes require valid authenticated provider configuration.

Publication also checks that the supplied proposal matches its saved per-run `result.json` and exactly matches that run's `proposal.patch`; changing a reviewer verdict in memory or swapping a patch between runs is rejected. The report itself is an operator-owned local artifact, not a cryptographic signature or a replacement for human review.

The full GitHub publication/Navigator route has **not yet undergone a live
authenticated author/reviewer test**. Local contract tests use a bare Git
remote and fake GitHub CLI output. A test that passes locally is not proof
that your credentials or external review wrapper work.

For manual publication, omit publication/navigation and inspect the local
patch first; use the existing CodeBridge Navigator after your usual PR
workflow.

## Reusable scheduled scans

Copy [the GitHub Actions template](https://github.com/selvaz/LazyTools/blob/feat/codemap-python-adapter/examples/maintenance-readonly-action.yml)
into your own workflow directory after pinning a trusted version. Its default
permissions are `contents: read` and it creates only scan artifacts: no
agent writes, PRs, merges or deployments. LazyCEO can invoke the same
`MaintenancePipeline` Python object on its own schedule, without creating
a second controller. The example also runs after every push to `main`, so mappings are refreshed promptly after accepted commits; adapt the branch name to the adopting repository.
