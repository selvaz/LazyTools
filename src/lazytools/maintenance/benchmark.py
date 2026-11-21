"""Paired coding benchmark: same RepoMap coordinator, different writer tool surface.

The A/B variable is ONLY whether the coding Agent can invoke codemap_*.
Both arms use a single pinned scan and the same objective, models, reviewer,
allowlisted files and checks. This module never publishes a PR or merges.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from lazybridge import Agent, Plan, Step, Tool, from_step

from .core import MaintenanceError, _atomic_json
from .improvement import ImprovementRequest, ImprovementTool, MaintenanceImprover
from .pipeline import MaintenancePipeline

ToolPairFactory = Callable[[bool], tuple[ImprovementTool, ImprovementTool]]
Order = Literal["baseline_first", "mapped_first"]


class PairedMaintenanceBenchmark:
    """Prepare a repeatable local A/B, deliberately disabled without approval."""

    def __init__(
        self,
        coordinator: MaintenancePipeline,
        request: ImprovementRequest,
        *,
        order: Order = "baseline_first",
    ) -> None:
        if coordinator.context_mode != "repomap":
            raise ValueError("Both arms require the SAME RepoMap-enabled coordinator")
        if coordinator.task_query != request.objective:
            raise ValueError("Coordinator task_query must match the approved objective")
        if not request.checks:
            raise ValueError("A paired benchmark requires operator-approved acceptance tests")
        if not request.writer_model or not request.reviewer_model:
            raise ValueError("Pin identical writer/reviewer models for both benchmark arms")
        if order not in ("baseline_first", "mapped_first"):
            raise ValueError("order must be baseline_first or mapped_first")
        self.coordinator = coordinator
        self.request = request
        self.order = order

    def build_agent(
        self, *, tools_factory: ToolPairFactory | None = None
    ) -> Agent:
        """Return an inspectable four-step LazyBridge Plan, without running it."""
        captured: dict[str, Any] = {}
        case_id = uuid.uuid4().hex
        out = self.coordinator.workspace.state_dir / "benchmarks" / case_id / "pair.json"
        arms = (
            ("baseline", False), ("repomap", True)
        ) if self.order == "baseline_first" else (
            ("repomap", True), ("baseline", False)
        )

        def scan(task: str) -> str:
            started = time.perf_counter()
            result = self.coordinator.execute()
            captured["scan"] = result
            captured["scan_seconds"] = round(time.perf_counter() - started, 4)
            return json.dumps({"report_json": result["report_json"]})

        def make_arm(name: str, mapped: bool):
            def execute(task: str) -> str:
                ref = json.loads(task)
                report = captured.get("scan")
                if not isinstance(report, dict) or ref.get("report_json") != report.get("report_json"):
                    raise MaintenanceError("Both arms must consume the same pinned scan")
                request = replace(
                    self.request, writer_repomap=mapped,
                    writer_repomap_executable=self.coordinator.executable,
                )
                injected = tools_factory(mapped) if tools_factory is not None else (None, None)
                start = time.perf_counter()
                outcome = MaintenanceImprover(self.coordinator.workspace).run(
                    report, request, writer_tool=injected[0], reviewer_tool=injected[1],
                )
                rounds = outcome.get("rounds", [])
                captured[name] = {
                    "arm": name,
                    "writer_repomap": mapped,
                    "configured_writer_tools": (
                        ["codemap_overview", "codemap_search",
                         "codemap_context", "codemap_impact"] if mapped else []
                    ),
                    "seconds": round(time.perf_counter() - start, 4),
                    "status": outcome["status"],
                    "verification": (
                        rounds[-1].get("verification", {}).get("status", "not_run")
                        if rounds else "not_run"
                    ),
                    "writer_usage": [row.get("writer_usage", {"source": "unavailable"})
                                     for row in rounds],
                    "rounds": len(rounds),
                    "first_writer_brief_sha256": (
                        rounds[0].get("writer_brief_sha256") if rounds else None
                    ),
                    "proposal_report": outcome["report_json"],
                    "patch_sha256": outcome.get("patch_sha256"),
                }
                return json.dumps({"arm": name, "status": outcome["status"]})
            return execute

        def finish(task: str) -> str:
            if not all(key in captured for key in ("scan", "baseline", "repomap")):
                raise MaintenanceError("The Plan did not complete both paired arms")
            scan_report = captured["scan"]
            left = captured["baseline"]["first_writer_brief_sha256"]
            right = captured["repomap"]["first_writer_brief_sha256"]
            if not isinstance(left, str) or not left or left != right:
                raise MaintenanceError(
                    "Writer briefs differed between arms; the A/B is invalid"
                )
            result = {
                "schema_version": 1, "case_id": case_id,
                "status": "comparison_ready", "order": self.order,
                "coordinator": {
                    "context_mode": "repomap",
                    "sha": scan_report["sha"],
                    "scan_run_id": scan_report["run_id"],
                    "scan_seconds": captured["scan_seconds"],
                    "task_context_sha256": hashlib.sha256(json.dumps(
                        scan_report.get("task_context", {}), sort_keys=True,
                    ).encode()).hexdigest(),
                },
                "writer_engine": self.request.writer_engine,
                "reviewer_engine": self.request.reviewer_engine,
                "writer_model": self.request.writer_model,
                "reviewer_model": self.request.reviewer_model,
                "arms": {"baseline": captured["baseline"], "repomap": captured["repomap"]},
                "limits": [
                    "Only writer tool access differs; coordinator uses RepoMap in BOTH arms.",
                    "Native engine token counters may be incomplete or zero.",
                    "No reviewer token estimate is fabricated when unavailable.",
                    "Wall time includes isolated writing, independent review and checks.",
                    "One pair is not a statistically significant benchmark.",
                ],
            }
            _atomic_json(out, result)
            return json.dumps({"report_json": str(out), "status": "comparison_ready"})

        tools = [Tool.wrap(scan, name="bench_scan")]
        steps = [Step("bench_scan")]
        for name, mapped in arms:
            step_name = f"bench_{name}"
            tools.append(Tool.wrap(make_arm(name, mapped), name=step_name))
            steps.append(Step(step_name, task=from_step("bench_scan")))
        tools.append(Tool.wrap(finish, name="bench_report"))
        steps.append(Step("bench_report"))
        return Agent(name="paired_maintenance_benchmark",
                     engine=Plan(*steps, max_iterations=len(steps) + 1),
                     tools=[*tools])

    def run(self, *, tools_factory: ToolPairFactory | None = None) -> dict[str, Any]:
        """Start real coding-agent turns only after explicit local approval."""
        if not self.request.approved_local_proposal:
            raise PermissionError("Benchmark requires explicit local-proposal approval")
        env = self.build_agent(tools_factory=tools_factory)(
            "Benchmark one approved task using the same pinned RepoMap scan"
        )
        if env.error is not None:
            raise MaintenanceError(f"Paired benchmark failed: {env.error.message[:400]}")
        locator = json.loads(env.text())
        if locator.get("status") != "comparison_ready":
            raise MaintenanceError("Paired benchmark did not complete")
        return json.loads(Path(locator["report_json"]).read_text(encoding="utf-8"))
