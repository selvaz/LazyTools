"""Declared LazyBridge Plan for read-only multi-repository maintenance.

The Plan chooses execution order; any nested reviewers receive bounded
structural evidence and cannot authorize code changes through this pipeline.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from lazybridge import Agent, Plan, Step, Tool, from_parallel_all, from_prev, from_step

from lazytools.codemap import RepoMapCLI

from .core import MaintenanceError, RunState, Workspace, _atomic_json
from .verification import CheckSpec, run_approved_checks

_STAGE_NAMES = {
    "maintenance_resolve", "maintenance_refresh", "maintenance_diff",
    "maintenance_impact", "maintenance_verify", "maintenance_report",
}


class MaintenancePipeline:
    """One fresh deterministic Plan and RunState per invocation.

    Optional reviewer Agents run *inside* the Plan as nested agent tools.
    They return advice, not test evidence or authorization. Untrusted review
    engines need a separate execution sandbox; this Plan never writes source.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        reviewers: Sequence[Agent] = (),
        checks: Sequence[CheckSpec] = (),
        executable: str = "repomap",
        use_embeddings: bool = False,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.workspace = workspace
        self.reviewers = tuple(reviewers)
        self.checks = tuple(checks)
        valid_repos = set(workspace.paths())
        if any(check.repo not in valid_repos for check in self.checks):
            raise ValueError("Approved checks must use configured repository aliases")
        if len({check.id for check in self.checks}) != len(self.checks):
            raise ValueError("Approved check ids must be unique")
        if len(self.checks) > 20:
            raise ValueError("At most 20 approved checks may run")
        names = [getattr(agent, "name", None) for agent in self.reviewers]
        if any(not isinstance(name, str) or not name.startswith("review_") for name in names):
            raise ValueError("Reviewers require distinct agent names beginning with 'review_'")
        if len(names) != len(set(names)) or set(names) & _STAGE_NAMES:
            raise ValueError("Reviewer names must be unique and cannot shadow steps")
        self.executable = executable
        self.use_embeddings = use_embeddings
        self.timeout_seconds = timeout_seconds

    def build_agent(self, state: RunState) -> Agent:
        """Return the inspectable Plan with deterministically named targets."""

        def resolve(task: str) -> str:
            return state.resolve()

        def refresh(task: str) -> str:
            return state.refresh()

        def diff(task: str) -> str:
            return state.diff()

        def impact(task: str) -> str:
            return state.impact()

        def verify(task: str) -> str:
            # The final reviewer output is advice, not proof of test success.
            state.review_text = task[:6000] if self.reviewers else None
            state.verification = run_approved_checks(state.workspace, state.sha, self.checks)
            return json.dumps({"verification": state.verification["status"]})

        def report(task: str) -> str:
            return state.finalize(state.review_text or "")

        tools = [
            Tool.wrap(resolve, name="maintenance_resolve",
                      description="Validate configured roots and pin immutable Git SHA vector."),
            Tool.wrap(refresh, name="maintenance_refresh",
                      description="Reuse or refresh commit-addressed local RepoMap indexes."),
            Tool.wrap(diff, name="maintenance_diff",
                      description="Diff commits, including renames, without changing code."),
            Tool.wrap(impact, name="maintenance_impact",
                      description="Produce bounded static impact evidence for review."),
            Tool.wrap(verify, name="maintenance_verify",
                      description="Run only explicitly approved test commands at pinned commits in temporary Git worktrees."),
            Tool.wrap(report, name="maintenance_report",
                      description="Write verified-provenance, unverified-conclusion JSON and Markdown."),
        ]
        steps: list[Step] = [
            Step("maintenance_resolve"),
            Step("maintenance_refresh"),
            Step("maintenance_diff"),
            Step("maintenance_impact"),
        ]
        if len(self.reviewers) == 1:
            steps.append(Step(self.reviewers[0].name, task=from_step("maintenance_impact")))
            steps.append(Step("maintenance_verify", task=from_prev))
        elif len(self.reviewers) > 1:
            for reviewer in self.reviewers:
                steps.append(
                    Step(reviewer.name, task=from_step("maintenance_impact"), parallel=True)
                )
            steps.append(
                Step("maintenance_verify", task=from_parallel_all(self.reviewers[0].name))
            )
        else:
            steps.append(Step("maintenance_verify"))
        steps.append(Step("maintenance_report"))
        return Agent(
            name="repository_maintenance",
            engine=Plan(*steps, max_iterations=len(steps) + 2),
            tools=[*tools, *self.reviewers],
        )

    def execute(self, *, base_refs: Mapping[str, str] | None = None) -> dict[str, Any]:
        base = dict(base_refs or {})
        unknown = set(base) - set(self.workspace.paths())
        if unknown:
            raise ValueError(f"Unknown repository in base_refs: {', '.join(sorted(unknown))}")
        mapper = RepoMapCLI(
            self.workspace.paths(), executable=self.executable,
            timeout_seconds=self.timeout_seconds, use_embeddings=self.use_embeddings,
        )
        state = RunState(self.workspace, base, mapper)
        agent = self.build_agent(state)
        result = agent("Perform a read-only scan of configured repositories")
        # The Plan may capture an exception in its Envelope. Only the report
        # written by the deterministic final step establishes completion.
        if state.report is None or state.report_path is None:
            error = getattr(result, "error", None)
            reason = (
                f"{getattr(error, 'type', type(error).__name__)}: "
                f"{getattr(error, 'message', str(error))}"
                if error is not None else getattr(result, "text", lambda: str(result))()
            )
            raise MaintenanceError(f"Maintenance Plan did not complete: {str(reason)[:600]}")
        error = result.error
        if error is not None:
            raise MaintenanceError(
                f"Maintenance Plan ended with {error.type}: {error.message[:400]}"
            )
        # Plan aggregates direct and nested agent tokens in distinct counters.
        # Cached tokens have engine-dependent semantics and are not summed.
        metadata = result.metadata
        state.report["usage"] = {
            "source": "lazybridge_envelope",
            "input_tokens": metadata.input_tokens,
            "output_tokens": metadata.output_tokens,
            "nested_input_tokens": metadata.nested_input_tokens,
            "nested_output_tokens": metadata.nested_output_tokens,
            "cache_read_input_tokens": metadata.cache_read_input_tokens,
            "cache_creation_input_tokens": metadata.cache_creation_input_tokens,
            "cost_usd": round(metadata.cost_usd + metadata.nested_cost_usd, 8),
            "note": "Cache counters are engine-specific; nested cache counters are not aggregated.",
        }
        _atomic_json(state.report_path, state.report)
        return {
            **state.report,
            "report_json": str(state.report_path),
            "report_markdown": str(state.report_path.with_suffix(".md")),
        }
