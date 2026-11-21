"""A declared LazyBridge Plan that scans, then proposes a local improvement.

No new code-review implementation or LLM planner: the writer and independent
reviewer are existing LazyTools code-support tool factories. This is a
deterministic two-step Plan; bounded repair rounds live inside its proposal
step, while existing CodeBridge Navigator handles published PRs.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from lazybridge import Agent, Plan, Step, Tool

from .core import MaintenanceError
from .improvement import ImprovementRequest, ImprovementTool, MaintenanceImprover
from .pipeline import MaintenancePipeline


class MaintenanceSelfImprovementPlan:
    """Operator-gated read-only scan -> disposable local proposal Plan."""

    def __init__(
        self,
        maintenance: MaintenancePipeline,
        request: ImprovementRequest,
        *,
        base_refs: Mapping[str, str] | None = None,
    ) -> None:
        self.maintenance = maintenance
        self.request = request
        self.base_refs = dict(base_refs or {})

    def build_agent(
        self,
        *,
        writer_tool: ImprovementTool | None = None,
        reviewer_tool: ImprovementTool | None = None,
    ) -> Agent:
        def scan(task: str) -> str:
            return json.dumps(
                self.maintenance.execute(base_refs=self.base_refs),
                ensure_ascii=False,
            )

        def propose(task: str) -> str:
            scan_result = json.loads(task)
            result = MaintenanceImprover(self.maintenance.workspace).run(
                scan_result,
                self.request,
                writer_tool=writer_tool,
                reviewer_tool=reviewer_tool,
            )
            return json.dumps(result, ensure_ascii=False)

        return Agent(
            name="repository_self_improvement",
            engine=Plan(
                Step("maintenance_scan"),
                Step("maintenance_propose"),
                max_iterations=3,
            ),
            tools=[
                Tool.wrap(scan, name="maintenance_scan",
                          description="Execute the read-only commit-pinned maintenance Plan."),
                Tool.wrap(propose, name="maintenance_propose",
                          description="Generate a bounded independent-engine local patch, never merge."),
            ],
        )

    def run(
        self,
        *,
        writer_tool: ImprovementTool | None = None,
        reviewer_tool: ImprovementTool | None = None,
    ) -> dict[str, Any]:
        if not self.request.approved_local_proposal:
            raise PermissionError("Explicit approval is required before running self-improvement")
        envelope = self.build_agent(writer_tool=writer_tool, reviewer_tool=reviewer_tool)(
            "Review the configured repositories and make only a local proposal"
        )
        if envelope.error is not None:
            raise MaintenanceError(
                f"Self-improvement Plan failed: {envelope.error.type}: "
                f"{envelope.error.message[:400]}"
            )
        try:
            result = json.loads(envelope.text())
        except (TypeError, ValueError) as exc:
            raise MaintenanceError("Self-improvement Plan did not return a valid report") from exc
        if not isinstance(result, dict) or "status" not in result:
            raise MaintenanceError("Self-improvement Plan returned no status")
        return result
