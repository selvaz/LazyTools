"""Atomic consumption and decision tests for the canonical task approval queue."""

from __future__ import annotations

from datetime import timedelta

import pytest
from lazybridge import Store
from lazybridge.ext import approval

from lazytools.projects.task_approvals import EffectAuthorization, ProjectTaskApprovals
from lazytools.projects.task_service import ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import TaskServiceError
from test_projects_task_service import (
    OPERATOR,
    OTHER_SHA,
    PROJECT,
    REVIEWER,
    SHA,
    WORKER,
    admit,
    claim,
    complete,
    evidence,
    parallel,
    placement,
    queue,  # noqa: F401
    submit,
    task,
)

OWNER = TaskPrincipal(actor="owner", role="owner", projects=(PROJECT,))
OUTSIDER = TaskPrincipal(actor="elsewhere", role="orchestrator", projects=("other-project",))


@pytest.fixture
def approvals(queue):  # noqa: F811
    if not hasattr(approval.ApprovalQueue, "create_typed_ticket"):
        pytest.skip("Requires LazyBridge main with the typed ApprovalQueue")
    projects, service = queue
    admit(projects, [
        task("a", repo="test/repo", assigned_branch="work"), task("b", dependency_ids=["a"]),
    ])
    placement(service, "a")
    receipt = claim(service, "a")
    job = submit(service, receipt)
    state = {"revision": "v1", "head": SHA, "verified_head": SHA}

    def authorize(principal, task_data, action, target):
        if target.sha is not None and target.sha != state["head"]:
            return None
        return EffectAuthorization(
            policy_revision=state["revision"], rule_id=f"rule-{action}", risk="medium",
            production=target.path == "production", verified_head=state["verified_head"],
        )

    return ProjectTaskApprovals(service, authorize=authorize), receipt, job, state


def target(**fields):
    return approval.ApprovalTarget(**{"repo": "test/repo", "ref": "refs/heads/work", "sha": SHA, **fields})


def request(registry, receipt, action="push", invocation="invocation", principal=WORKER, **fields):
    return registry.create_request(
        principal, receipt["task_id"], receipt["claim_token"], invocation, action, target(**fields),
    )


def decide(registry, req, principal=OPERATOR, verdict="allow", reason="Reviewed exact effect"):
    return registry.decide(principal, req["request_id"], req["action_digest"], verdict, reason)


def consume(registry, receipt, req, key="effect", principal=WORKER, action=None, observed=None):
    registry.consume(
        principal, req["request_id"], receipt["claim_token"], key, req["action_digest"],
        action if action is not None else req["action"],
        observed if observed is not None else approval.ApprovalTarget(**req["target"]),
    )


def test_canonical_persistence_scopes_and_idempotent_decisions(approvals):
    registry, receipt, _, _ = approvals
    req = request(registry, receipt)
    other = request(registry, receipt, action="open_pr", invocation="studio-tool")
    assert request(registry, receipt) == req
    assert registry.pending(REVIEWER)["requests"] == []
    assert registry.pending(OUTSIDER)["requests"] == []
    assert registry.pending(OWNER)["requests"] == []
    assert len(registry.pending(WORKER, PROJECT, receipt["task_id"])["requests"]) == 2
    page = registry.pending(OPERATOR, limit=1)
    following = registry.pending(OPERATOR, limit=1, cursor=page["next_cursor"])
    assert {page["requests"][0]["request_id"], following["requests"][0]["request_id"]} == {
        req["request_id"], other["request_id"],
    }
    with pytest.raises(TaskServiceError):
        decide(registry, req, principal=REVIEWER)
    with pytest.raises(TaskServiceError):
        decide(registry, req, principal=OUTSIDER)
    with pytest.raises(TaskServiceError, match="digest"):
        registry.decide(OPERATOR, req["request_id"], "incorrect", "allow", "Verified")
    stored = decide(registry, req)
    assert decide(registry, req) == stored
    with pytest.raises(TaskServiceError, match="different decision"):
        decide(registry, req, verdict="deny")
    bridge_adapter = ProjectTaskApprovals(ProjectTaskService(str(registry.service.path)))
    assert bridge_adapter.get(WORKER, req["request_id"]) == stored["request"]
    with Store(db=str(registry.service.path)) as store:
        queue = approval.ApprovalQueue(store)
        canonical = queue.get_ticket(req["request_id"])
        assert canonical.task_id == receipt["task_id"]
        assert canonical.decision.decider == OPERATOR.actor
        assert not queue.approve_ticket(other["request_id"], actor="legacy", channel="legacy")
        legacy = queue.create_ticket(task_id=receipt["task_id"], prompt="Legacy prompt")
    assert [entry["request_id"] for entry in registry.pending(OPERATOR)["requests"]] == [other["request_id"]]
    with pytest.raises(TaskServiceError, match="Unknown governed"):
        registry.decide(OPERATOR, legacy.approval_id, legacy.prompt_hash, "allow", "Legacy")


@pytest.mark.parametrize(("action", "required", "fields"), [
    ("read", "automatic", {}),
    ("local_commit", "automatic", {}),
    ("push", "orchestrator", {}),
    ("open_pr", "orchestrator", {}),
    ("scoped_delete", "orchestrator", {"path": "scratch/output"}),
    ("scoped_delete", "owner", {"path": "production"}),
    ("production_change", "owner", {}),
])
def test_effect_policy_rows(approvals, action, required, fields):
    registry, receipt, _, _ = approvals
    req = request(registry, receipt, action, **fields)
    assert req["required_approver"] == required
    if required != "automatic":
        assert req["status"] == "pending"
        with pytest.raises(TaskServiceError, match="unapproved"):
            consume(registry, receipt, req)
        decide(registry, req, principal=OWNER if required == "owner" else OPERATOR)
    else:
        assert req["status"] == "approved" and req["decision"]["reason"] == req["rule_id"]
    consume(registry, receipt, req)
    assert registry.get(WORKER, req["request_id"])["effect_unresolved"]


def test_policy_refuses_unknown_effect_unbound_worker_and_head_drift(approvals):
    registry, receipt, _, state = approvals
    for action in ("unsafe_shell", "merge_main"):
        with pytest.raises(TaskServiceError):
            request(registry, receipt, action)
    with pytest.raises(TaskServiceError, match="effective grant"):
        request(ProjectTaskApprovals(registry.service), receipt)
    req = request(registry, receipt)
    decide(registry, req)
    for options in (
        {"observed": target(ref="refs/heads/other")},
        {"observed": target(sha=OTHER_SHA)},
        {"action": "open_pr"},
        {"principal": REVIEWER},
    ):
        with pytest.raises(TaskServiceError):
            consume(registry, receipt, req, **options)
    state["revision"] = "v2"
    with pytest.raises(TaskServiceError, match="policy differs"):
        consume(registry, receipt, req)
    state["revision"] = "v1"
    state["head"] = OTHER_SHA
    with pytest.raises(TaskServiceError, match="effective grant"):
        consume(registry, receipt, req)
    state["head"] = SHA
    consume(registry, receipt, req)


def test_main_update_requires_orchestrator_and_current_head(approvals):
    registry, receipt, job, state = approvals
    service = registry.service
    service.clock.now += 61
    service.reconcile_dispatch(
        OPERATOR, receipt["task_id"], receipt["attempt"], "terminated", job_ref=job, evidence_ref="terminal"
    )
    receipt = claim(service, "a", "main-update", OPERATOR)
    submit(service, receipt, OPERATOR)
    req = request(registry, receipt, "merge_main", principal=OPERATOR, ref="refs/heads/main")
    decide(registry, req)
    state["head"] = OTHER_SHA
    with pytest.raises(TaskServiceError):
        consume(registry, receipt, req, principal=OPERATOR)
    state["head"] = SHA
    state["verified_head"] = OTHER_SHA
    with pytest.raises(TaskServiceError, match="checks and review"):
        consume(registry, receipt, req, principal=OPERATOR)
    state["verified_head"] = SHA
    consume(registry, receipt, req, principal=OPERATOR)


def test_deny_and_expire_approvals(approvals):
    registry, receipt, _, _ = approvals
    req = request(registry, receipt)
    decide(registry, req, verdict="deny")
    with pytest.raises(TaskServiceError, match="denied"):
        request(registry, receipt, invocation="try-again")
    expiring = registry.create_request(
        WORKER, receipt["task_id"], receipt["claim_token"], "expiry", "open_pr", target(),
        ttl=timedelta(seconds=1),
    )
    decide(registry, expiring)
    registry.service.clock.now += 2
    with pytest.raises(TaskServiceError, match="expired"):
        consume(registry, receipt, expiring)


def test_stale_attempt_cannot_consume(approvals):
    registry, receipt, job, _ = approvals
    req = request(registry, receipt)
    decide(registry, req)
    registry.service.clock.now += 61
    outcomes = parallel(
        lambda: consume(registry, receipt, req),
        lambda: registry.service.reconcile_dispatch(
            OPERATOR, receipt["task_id"], receipt["attempt"], "terminated", job_ref=job, evidence_ref="terminal"
        ),
    )
    assert outcomes == ["stale", None]
    new = claim(registry.service, "a", "new", REVIEWER)
    with pytest.raises(TaskServiceError, match="active claim"):
        consume(registry, new, req, principal=REVIEWER)


@pytest.mark.parametrize("result", ["succeeded", "not_applied"])
def test_raced_consumption_and_interruption_hold_effect_until_reconciled(approvals, result):
    registry, receipt, job, _ = approvals
    req = request(registry, receipt)
    decide(registry, req)
    outcomes = parallel(
        lambda: consume(registry, receipt, req, "first"),
        lambda: consume(registry, receipt, req, "second"),
    )
    assert outcomes.count(None) == 1 and outcomes.count("unresolved") == 1
    spent_key = ("first", "second")[outcomes.index(None)]
    registry = ProjectTaskApprovals(
        ProjectTaskService(str(registry.service.path), clock=registry.service.clock), authorize=registry.authorize,
    )
    assert registry.get(WORKER, req["request_id"])["effect_unresolved"]
    ref = evidence(registry.service, receipt, job)
    with pytest.raises(TaskServiceError, match="outstanding effects"):
        complete(registry.service, receipt, ref)
    with pytest.raises(TaskServiceError, match="outstanding effect"):
        request(registry, receipt, invocation="repeat-effect")
    outcome = registry.record_outcome(OPERATOR, req["request_id"], spent_key, result, "provider-observation")
    assert not outcome["effect_unresolved"]
    assert registry.record_outcome(OPERATOR, req["request_id"], spent_key, result, "provider-observation") == outcome
    if result == "succeeded":
        with pytest.raises(TaskServiceError, match="already succeeded"):
            request(registry, receipt, invocation="repeat-effect")
    else:
        retry = request(registry, receipt, invocation="repeat-effect")
        decide(registry, retry)
        with pytest.raises(TaskServiceError, match="already been spent"):
            consume(registry, receipt, retry, spent_key)
        consume(registry, receipt, retry, "fresh-effect")
