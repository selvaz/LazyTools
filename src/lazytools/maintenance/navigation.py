"""Optional published-PR follow-up through the EXISTING CodeBridge Navigator.

This module does not reimplement the review/fix loop or merge policy.
Review commands and checks are trusted, operator-injected Python arguments,
never values read from a repository, PR body, LLM output or TOML manifest.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .core import MaintenanceError
from .improvement import ImprovementRequest


@dataclass(frozen=True)
class NavigationApproval:
    reviewer_command: str
    check_commands: tuple[str, ...]
    approved_navigate: bool = False
    max_rounds: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.reviewer_command, str) or not self.reviewer_command.strip():
            raise ValueError("A trusted independent reviewer command is required")
        if not isinstance(self.check_commands, tuple) or not 1 <= len(self.check_commands) <= 20:
            raise ValueError("Supply 1-20 trusted required check commands")
        if any(not isinstance(s, str) or not s.strip() or len(s) > 500 for s in self.check_commands):
            raise ValueError("Invalid required check command")
        if not 1 <= self.max_rounds <= 3:
            raise ValueError("Navigator rounds must be 1-3")


def navigate_published_pr(
    publication: dict[str, Any],
    request: ImprovementRequest,
    approval: NavigationApproval,
) -> dict[str, Any]:
    """Review exact pushed PR head using CodeBridge's existing state machine.

    The Navigator can relaunch coding fixes on a PR, but never merges.
    This needs an authenticated gh + coding provider and a trusted reviewer
    command that posts the independent-review marker on the PR.
    """
    if not approval.approved_navigate:
        raise PermissionError("An explicit Navigator permission is required")
    if publication.get("status") != "draft_pr_created":
        raise MaintenanceError("No published draft PR to navigate")
    if request.writer_engine == request.reviewer_engine:
        raise MaintenanceError("Independent engine family is required")
    if publication.get("reviewer_engine") != request.reviewer_engine:
        raise MaintenanceError("Unexpected reviewer engine in publication")
    repo, number = publication.get("github_repo"), publication.get("pr_number")
    if not isinstance(repo, str) or not isinstance(number, int) or number < 1:
        raise MaintenanceError("Published PR identity is incomplete")

    from lazytools.code_bridge._navigator import Navigator, ProductionBackend

    backend = ProductionBackend(
        repo=repo, pr_number=number, reviewer_cmd=approval.reviewer_command,
        fix_engine=request.writer_engine,
    )
    record = Navigator(
        backend, checks=approval.check_commands, max_rounds=approval.max_rounds,
    ).run("")
    return {
        **publication,
        "status": "merge_ready" if record.status == "merge-ready" else "navigator_" + record.status,
        "navigator": asdict(record),
        "merged": False,
        "requires_human_merge": True,
    }
