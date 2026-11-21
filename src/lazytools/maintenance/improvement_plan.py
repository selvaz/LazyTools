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
from .handoff import PublicationApproval, publish_draft_pr
from .improvement import ImprovementRequest, ImprovementTool, MaintenanceImprover
from .navigation import NavigationApproval, navigate_published_pr
from .pipeline import MaintenancePipeline


class MaintenanceSelfImprovementPlan:
    """Operator-gated read-only scan -> disposable local proposal Plan."""

    def __init__(
        self,
        maintenance: MaintenancePipeline,
        request: ImprovementRequest,
        *,
        base_refs: Mapping[str, str] | None = None,
        publication: PublicationApproval | None = None,
        navigation: NavigationApproval | None = None,
    ) -> None:
        if navigation is not None and publication is None:
            raise ValueError("Navigator requires a configured publication stage")
        self.publication = publication
        self.navigation = navigation
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

        steps = [Step("maintenance_scan"), Step("maintenance_propose")]
        tools = [
            Tool.wrap(scan, name="maintenance_scan",
                      description="Execute the read-only commit-pinned maintenance Plan."),
            Tool.wrap(propose, name="maintenance_propose",
                      description="Generate a bounded independent-engine local patch, never merge."),
        ]

        publication = self.publication
        if publication is not None:
            def publish(task: str) -> str:
                proposal = json.loads(task)
                if proposal.get("status") != "proposal_ready_for_human_review":
                    return json.dumps(proposal)
                return json.dumps(publish_draft_pr(
                    self.maintenance.workspace, self.request, proposal, publication
                ))

            steps.append(Step("maintenance_publish"))
            tools.append(Tool.wrap(publish, name="maintenance_publish",
                                   description="Explicitly authorized draft-PR handoff only."))

        navigation = self.navigation
        if navigation is not None:
            def navigate(task: str) -> str:
                published = json.loads(task)
                if published.get("status") != "draft_pr_created":
                    return json.dumps(published)
                return json.dumps(navigate_published_pr(
                    published, self.request, navigation
                ))

            steps.append(Step("maintenance_navigate"))
            tools.append(Tool.wrap(navigate, name="maintenance_navigate",
                                   description="Reuse CodeBridge Navigator; never merge."))

        return Agent(
            name="repository_self_improvement",
            engine=Plan(*steps, max_iterations=len(steps) + 1),
            tools=[*tools],
        )

    def run(
        self,
        *,
        writer_tool: ImprovementTool | None = None,
        reviewer_tool: ImprovementTool | None = None,
    ) -> dict[str, Any]:
        if not self.request.approved_local_proposal:
            raise PermissionError("Explicit approval is required before running self-improvement")
        if self.publication is not None and not self.publication.approved_publish_draft:
            raise PermissionError("Publishing a draft PR requires separate explicit approval")
        if self.navigation is not None and not self.navigation.approved_navigate:
            raise PermissionError("CodeBridge Navigator requires separate explicit approval")
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
