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
PYTHONPATH=src python -m pytest -q tests/test_codemap.py tests/test_maintenance.py
~~~

GitHub CI adds a real RepoMap binary smoke with two successive Git commits, first/second snapshots and a verified cache hit. Ruff, mypy, multiple Python versions and MCP majors run in the regular LazyTools matrix.

**Not yet included:** GitHub event triggers, safe patch/PR generation, full cross-repo behavioral compatibility, durable Plan checkpoints and idempotent locking, arbitrary-language coverage, remote-head freshness checks, and end-to-end coding-agent token/time savings. These are defined as future phases in the [full spec](repo-maintenance-pipeline-spec.md).
