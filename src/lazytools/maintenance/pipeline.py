"""Declared LazyBridge Plan for read-only multi-repository maintenance.

The Plan chooses execution order; any nested reviewers receive bounded
structural evidence and cannot authorize code changes through this pipeline.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from lazybridge import Agent, Plan, Step, Tool, from_parallel_all, from_prev, from_step

from lazytools.codemap import RepoMapCLI

from .core import MaintenanceError, RunState, Workspace

_STAGE_NAMES = {
    "maintenance_resolve", "maintenance_refresh", "maintenance_diff",
    "maintenance_impact", "maintenance_report",
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
        executable: str = "repomap",
        use_embeddings: bool = False,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.workspace = workspace
        self.reviewers = tuple(reviewers)
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

        def resolve(_input: str = "") -> str:
            return state.resolve()

        def refresh(_input: str = "") -> str:
            return state.refresh()

        def diff(_input: str = "") -> str:
            return state.diff()

        def impact(_input: str = "") -> str:
            return state.impact()

        def report(review_text: str = "") -> str:
            return state.finalize(review_text if self.reviewers else "")

        tools = [
            Tool.wrap(resolve, name="maintenance_resolve",
                      description="Validate configured roots and pin immutable Git SHA vector."),
            Tool.wrap(refresh, name="maintenance_refresh",
                      description="Reuse or refresh commit-addressed local RepoMap indexes."),
            Tool.wrap(diff, name="maintenance_diff",
                      description="Diff commits, including renames, without changing code."),
            Tool.wrap(impact, name="maintenance_impact",
                      description="Produce bounded static impact evidence for review."),
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
            steps.append(Step("maintenance_report", task=from_prev))
        elif len(self.reviewers) > 1:
            for reviewer in self.reviewers:
                steps.append(
                    Step(reviewer.name, task=from_step("maintenance_impact"), parallel=True)
                )
            steps.append(
                Step("maintenance_report", task=from_parallel_all(self.reviewers[0].name))
            )
        else:
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
            message = getattr(result, "text", lambda: str(result))()
            raise MaintenanceError(f"Maintenance Plan did not complete: {str(message)[:600]}")
        return {
            **state.report,
            "report_json": str(state.report_path),
            "report_markdown": str(state.report_path.with_suffix(".md")),
        }
