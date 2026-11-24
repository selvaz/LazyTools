"""One-time, commit-bound draft pull request publication."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from lazybridge import Store
from lazybridge.ext.approval import ApprovalQueue, ApprovalTarget, ApprovalTicket, ExecutorContext, TypedApprovalRequest

from lazytools.code_bridge import _cloud, _gh, _registry
from lazytools.projects.task_service import ProjectTaskService, PublicationReview
from lazytools.projects.task_store import (
    TaskServiceError,
    claim_ticket_publication,
    publication_for_ticket,
    transaction,
)

_EXECUTOR = "local-publication-executor"
_ACTION = "open_draft_pr"
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_REPOSITORY = re.compile(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+")
_GIT_ERRORS = (_cloud.CloudError, OSError, subprocess.SubprocessError)


@dataclass(frozen=True)
class PublicationAttempt:
    task_id: str
    repo: Path
    repository: str
    remote: str
    head_branch: str
    base_branch: str
    head_sha: str
    review: PublicationReview
    local_head: str | None


@dataclass(frozen=True)
class PublicationOutcome:
    reason: str | None = None
    ticket: ApprovalTicket | None = None
    consumed: bool = False
    stored_receipt: str | None = None
    base_sha: str = ""
    pr_number: int | None = None
    pr_url: str | None = None
    observed_head: str | None = None


def _remote_repository(url: str) -> str | None:
    if "://" in url:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.hostname != "github.com"
            or parsed.port not in (None, 443 if parsed.scheme == "https" else 22)
            or parsed.query
            or parsed.fragment
        ):
            return None
        path = parsed.path.removeprefix("/")
    elif url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        return None
    slug = path.rstrip("/").removesuffix(".git")
    return slug.lower() if _REPOSITORY.fullmatch(slug) else None


def _branch_sha(repo: Path, branch: str) -> str | None:
    head = _cloud._git(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", check=False)
    return head if _SHA.fullmatch(head) else None


def prepare(
    service: ProjectTaskService,
    task_id: str,
    repo: Path,
    head: str,
    base: str,
) -> PublicationAttempt:
    try:
        review = service.publication_review(task_id)
    except TaskServiceError as exc:
        if exc.code != "not_found":
            raise
        review = PublicationReview("", "")
    for branch in (head, base):
        if branch.startswith("-"):
            raise ValueError("Publication requires a valid branch name")
        _cloud._git(repo, "check-ref-format", f"refs/heads/{branch}")
    if head == base:
        raise ValueError("Publication requires distinct head and base branches")
    fetch = _cloud._git(repo, "remote", "get-url", "--all", "origin").splitlines()
    push = _cloud._git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()
    if len(fetch) != 1 or len(push) != 1 or not (repository := _remote_repository(push[0])):
        raise ValueError("Publication requires one GitHub origin push URL")
    if _remote_repository(fetch[0]) != repository:
        raise ValueError("The origin fetch and push URLs refer to different repositories")
    local_head = _branch_sha(repo, head)
    candidate = review.reviewer.evidence.reviewed_sha if review.reviewer else None
    if candidate is None and review.writer:
        candidate = review.writer.evidence.artifact_ref
    return PublicationAttempt(
        task_id, repo, repository, push[0], head, base, candidate or local_head or "", review, local_head
    )


def _refusal(attempt: PublicationAttempt) -> str | None:
    review = attempt.review
    if review.writer is None or review.reviewer is None:
        return "review_missing"
    if review.independence is None:
        return "review_not_independent"
    if attempt.head_sha != attempt.local_head:
        return "head_changed_after_review"
    if any((check.model_extra or {}).get("head_sha", attempt.head_sha) != attempt.head_sha for check in review.checks):
        return "check_head_mismatch"
    if review.repository.casefold() != attempt.repository or (
        review.branch and review.branch.removeprefix("refs/heads/") != attempt.head_branch
    ):
        return "approval_target_mismatch"
    return None


def _request(attempt: PublicationAttempt, policy_revision: str, policy_digest: str) -> TypedApprovalRequest:
    writer, reviewer = attempt.review.writer, attempt.review.reviewer
    assert writer is not None and reviewer is not None
    return TypedApprovalRequest.create(
        task_id=writer.task_id,
        attempt=writer.attempt,
        action=_ACTION,
        target=ApprovalTarget(repo=attempt.repository, ref=attempt.head_branch, sha=attempt.head_sha),
        requested_by=_EXECUTOR,
        policy_revision=policy_revision,
        payload={
            "registry": "code-bridge",
            "publication": True,
            "base_branch": attempt.base_branch,
            "writer_job": writer.job_ref,
            "task_revision": writer.revision,
            "review_job": reviewer.job_ref,
            "review_evidence": reviewer.evidence.reference,
            "policy_digest": policy_digest,
        },
    )


def _find_ticket(
    registry: _registry.Registry, request: TypedApprovalRequest, selected_id: str | None
) -> ApprovalTicket | None:
    if selected_id is not None:
        return registry.inspect(selected_id)
    # File or recover one ticket under the same lock as its request index.
    assert registry.path is not None
    with _registry.registry_store(registry.path):
        pass
    with transaction(registry.path, write=True) as store:
        queue = ApprovalQueue(cast(Store, store))
        key = f"publication:request:{request.digest}"
        ticket_id = store.read(key)
        if ticket_id is not None:
            existing = queue.get_ticket(ticket_id)
            if existing is None or existing.consumed_at or existing.status == "rejected":
                return existing
            if existing.expires_at > datetime.now(UTC):
                return existing
        target = request.target
        ticket = queue.create_typed_ticket(
            request,
            prompt=f"Open draft PR in {target.repo} from {target.ref} at {target.sha} into {request.payload['base_branch']}",
            operator_only=True,
        )
        store.write(key, ticket.approval_id)
        return ticket


def _authorize(
    service: ProjectTaskService,
    registry: _registry.Registry,
    attempt: PublicationAttempt,
    request: TypedApprovalRequest,
    ticket: ApprovalTicket,
) -> PublicationOutcome:
    if ticket.request != request or not ticket.operator_only:
        return PublicationOutcome("approval_target_mismatch", ticket=ticket)
    executor = ExecutorContext(
        actor=_EXECUTOR,
        task_id=request.task_id,
        attempt=request.attempt,
        digest=request.digest,
        policy_revision=request.policy_revision,
        target=request.target,
    )
    separate_queue = registry.queue if registry.path != service.path else None
    with transaction(service.path, write=True) as store:
        claimed, saved = publication_for_ticket(store, ticket.approval_id)
        if saved is not None:
            return PublicationOutcome(ticket=ticket, stored_receipt=saved)
        if claimed:
            return PublicationOutcome("approval_already_consumed", ticket=ticket)
        queue = separate_queue if separate_queue is not None else ApprovalQueue(cast(Store, store))
        fresh = queue.get_ticket(ticket.approval_id)
        if fresh is None or fresh.request != request or not fresh.operator_only:
            return PublicationOutcome("approval_target_mismatch", ticket=fresh)
        if fresh.consumed_at is not None:
            return PublicationOutcome("approval_already_consumed", ticket=fresh)
        if fresh.status == "rejected":
            return PublicationOutcome("approval_rejected", ticket=fresh)
        if fresh.status != "approved" or fresh.expires_at <= datetime.now(UTC):
            return PublicationOutcome("approval_not_decided", ticket=fresh)
        if service.publication_review(attempt.task_id, store=store) != attempt.review:
            return PublicationOutcome("review_changed_before_publication", ticket=fresh)
        local_head = _branch_sha(attempt.repo, attempt.head_branch)
        if local_head != attempt.head_sha:
            return PublicationOutcome("head_changed_after_review", ticket=fresh, observed_head=local_head or "")
        if not queue.consume_typed_ticket(fresh.approval_id, executor=executor):
            return PublicationOutcome("approval_already_consumed", ticket=fresh)
        claim_ticket_publication(store, fresh.approval_id)
        return PublicationOutcome(ticket=queue.get_ticket(fresh.approval_id), consumed=True)


def _publish(attempt: PublicationAttempt, outcome: PublicationOutcome) -> PublicationOutcome:
    repo, head, base = f"github.com/{attempt.repository}", attempt.head_branch, attempt.base_branch
    try:
        # The push source is the reviewed object ID even if the local ref moves
        # after the final check. A literal URL avoids named remote push refspecs.
        _cloud._git(
            attempt.repo,
            "-c",
            "push.followTags=false",
            "push",
            "--",
            attempt.remote,
            f"{attempt.head_sha}:refs/heads/{head}",
            timeout=120,
        )
    except _GIT_ERRORS:
        return replace(outcome, reason="push_failed")
    try:
        url = _gh.pr_create(repo, base=base, head=head, title=f"Publish {head}", body="", draft=True)
        number = _gh.pr_number_from_url(url)
        if number is None or url.casefold() != f"https://{repo}/pull/{number}":
            return replace(outcome, reason="pr_create_failed")
    except _GIT_ERRORS:
        return replace(outcome, reason="pr_create_failed")
    try:
        pr = _gh.pr_view(repo, number, publication=True)
    except _GIT_ERRORS:
        return replace(outcome, reason="pr_head_unavailable")
    pr_head = pr.get("headRefOid")
    if (
        pr_head != attempt.head_sha
        or pr.get("headRefName") != head
        or pr.get("baseRefName") != base
        or pr.get("isCrossRepository") is not False
    ):
        observed_head = pr_head if isinstance(pr_head, str) and _SHA.fullmatch(pr_head) else ""
        return replace(outcome, reason="pr_head_mismatch", observed_head=observed_head)
    if pr.get("isDraft") is not True or pr.get("state") != "OPEN":
        return replace(outcome, reason="pr_not_draft")
    base_sha = pr.get("baseRefOid")
    if not isinstance(base_sha, str) or not _SHA.fullmatch(base_sha):
        return replace(outcome, reason="pr_head_unavailable")
    return replace(outcome, base_sha=base_sha, pr_number=number, pr_url=url)


def run(
    service: ProjectTaskService,
    registry: _registry.Registry,
    attempt: PublicationAttempt,
    *,
    policy_revision: str,
    policy_digest: str,
    ticket_id: str | None,
) -> PublicationOutcome:
    if reason := _refusal(attempt):
        return PublicationOutcome(reason)
    request = _request(attempt, policy_revision, policy_digest)
    ticket = _find_ticket(registry, request, ticket_id)
    if ticket is None:
        return PublicationOutcome("approval_target_mismatch")
    outcome = _authorize(service, registry, attempt, request, ticket)
    return _publish(attempt, outcome) if outcome.consumed else outcome
