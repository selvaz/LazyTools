"""Python-native repository maintenance and explicitly gated improvements.

The read-only Plan runs without MCP or LazyCEO; publication and coding
capabilities are optional, independently authorized integrations.
"""
from __future__ import annotations

from .benchmark import PairedMaintenanceBenchmark
from .core import Dependency, MaintenanceError, RepoSpec, Workspace
from .handoff import PublicationApproval, publish_draft_pr
from .improvement import ImprovementRequest, MaintenanceImprover
from .improvement_plan import MaintenanceSelfImprovementPlan
from .navigation import NavigationApproval, navigate_published_pr
from .pipeline import MaintenancePipeline
from .verification import CheckSpec

__all__ = [
    "CheckSpec",
    "Dependency",
    "ImprovementRequest",
    "MaintenanceError",
    "MaintenanceImprover",
    "MaintenancePipeline",
    "MaintenanceSelfImprovementPlan",
    "NavigationApproval",
    "PairedMaintenanceBenchmark",
    "PublicationApproval",
    "RepoSpec",
    "Workspace",
    "navigate_published_pr",
    "publish_draft_pr",
]
