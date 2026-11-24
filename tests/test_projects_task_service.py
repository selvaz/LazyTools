"""Acceptance tests for the governed task service and projects MCP endpoints."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from lazybridge import Store

from lazytools.connectors.projects import ProjectsTools
from lazytools.projects import contracts, verification
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_service import CompletionEvidence, PlacementEvidence, ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import TaskServiceError

PROJECT = "queue-test"
WORKER = TaskPrincipal(actor="writer", role="worker", projects=(PROJECT,))
REVIEWER = TaskPrincipal(actor="reviewer", role="worker", projects=(PROJECT,))
OPERATOR = TaskPrincipal(actor="operator", role="orchestrator", projects=(PROJECT,))
SHA = "a" * 40
OTHER_SHA = "b" * 40


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.delenv("LAZYTOOLS_PROJECTS_STORE_DB", raising=False)
    monkeypatch.delenv("LAZYCEO_CEO_STORE_DB", raising=False)
    path = str(tmp_path / "projects.sqlite")
    projects = ProjectsTools(store_db=path, allow_write=True)
    assert "AS A DRAFT" in projects.projects_create(PROJECT, "Queue tests", "Test the task service")
    service = ProjectTaskService(path, lease_seconds=60, clock=Clock())
    return projects, service


def task(identifier, tier="basic", **fields):
    return {
        "text": f"Produce {identifier}", "task_id": identifier, "tier": tier,
        "acceptance_criteria": [f"The {identifier} artifact exists"], **fields,
    }


def admit(projects, tasks):
    args = (PROJECT, "Artifacts are visible", "2027-01-01T00:00:00Z", tasks)
    verdict = projects.projects_review_plan(*args)
    assert verdict["ok"], verdict
    assert projects.projects_promote(*args).startswith("promoted")


def revision(service, identifier):
    with Store(db=str(service.path)) as store:
        for index, entry in enumerate(store.read(_board_key(PROJECT))["tasks"]):
            if entry.get("task_id", f"@{index}") == identifier:
                return entry.get("revision", "0")
    raise AssertionError(f"No task named {identifier}")


def placement(service, identifier, kind="local", *, eligible=True, reason="Runner verified", ttl=1000, engine="codex"):
    service.record_placement(
        f"{PROJECT}/{identifier}", revision(service, identifier),
        PlacementEvidence(
            placement=kind, eligible=eligible, reason=reason, reference=f"preflight-{identifier}-{kind}",
            expires_at=service.clock() + ttl, engine_family=engine,
            environment_id="cloud-env" if kind == "cloud" else None,
        ),
    )


def claim(service, identifier, key="dispatch", principal=WORKER):
    return service.claim(principal, f"{PROJECT}/{identifier}", revision(service, identifier), key)["claim"]


def submit(service, receipt, principal=WORKER):
    job = f"job-{receipt['idempotency_key']}"
    service.begin_submit(principal, receipt["task_id"], receipt["claim_token"])
    service.record_submit(principal, receipt["task_id"], receipt["claim_token"], job)
    return job


def evidence(service, receipt, job, **fields):
    values = {
        "reference": f"evidence-{job}", "job_ref": job, "executor": receipt["executor"],
        "engine_family": receipt["engine_family"], "state": "accepted", "artifact_ref": SHA,
        **fields,
    }
    service.record_completion_evidence(
        OPERATOR, receipt["task_id"], receipt["attempt"], CompletionEvidence(**values)
    )
    return values["reference"]


def complete(service, receipt, ref, principal=WORKER, key="complete", state="accepted"):
    return service.complete(principal, receipt["task_id"], receipt["claim_token"], key, state, ref)


def ready(service):
    return service.next_ready(OPERATOR, PROJECT)["tasks"]


def test_queue_metrics_groups_and_blocked_reasons(queue):
    projects, service = queue
    admit(projects, [task("a"), task("b", "writing", requested_placement="cloud"),
                     task("waiting", dependency_ids=["a"]), task("held"), task("missing"),
                     task("failed"), task("after_failure", dependency_ids=["failed"])])
    for name in ("a", "waiting", "held", "failed", "after_failure"):
        placement(service, name)
    placement(service, "b", "cloud")
    assert "scheduled" in projects.projects_schedule_task(PROJECT, 3, "Produce held", "Waiting for input", hold=True)
    receipt = claim(service, "failed", "failure")
    complete(service, receipt, evidence(service, receipt, submit(service, receipt), state="blocked"), state="blocked")
    service.clock.now += 10
    metrics = service.queue_metrics(OPERATOR)
    view = metrics["projects"][PROJECT]
    assert metrics["overall"] == view
    assert {(g["tier"], g["effective_placement"], g["count"], g["oldest_ready_age_seconds"]) for g in view["ready"]} == {
        ("basic", "local", 1, 10), ("writing", "cloud", 1, 10)}
    assert view["blocked"] == {"unmet_dependency": 1, "failed_dependency": 1, "hold": 1,
                               "missing_placement_evidence": 1, "route_quarantined": 0}
    claim(service, "a")
    assert service.queue_metrics(OPERATOR)["overall"]["running"] == 1


def test_fallback_ready_age_survives_unchanged_negative_cloud_checks(queue):
    projects, service = queue
    admit(projects, [task("a", requested_placement="cloud"), task("b")])
    placement(service, "a")
    placement(service, "a", "cloud", eligible=False)
    service.clock.now += 100
    placement(service, "a", "cloud", eligible=False)
    group = service.queue_metrics(OPERATOR)["overall"]["ready"][0]
    assert group["effective_placement"] == "local"
    assert group["oldest_ready_age_seconds"] == 100
    placement(service, "a", "cloud")
    service.clock.now += 10
    placement(service, "a", "cloud", eligible=False)
    assert service.queue_metrics(OPERATOR)["overall"]["ready"][0]["oldest_ready_age_seconds"] == 0


def test_cloud_without_environment_fails_closed_and_local_fallback_is_independent(queue):
    projects, service = queue
    admit(projects, [task("a", requested_placement="cloud", repo="org/repo"), task("b")])
    def unidentified_cloud():
        service.record_placement(f"{PROJECT}/a", revision(service, "a"), PlacementEvidence(
            placement="cloud", eligible=True, reason="Green", reference="cloud-check",
            expires_at=service.clock() + 1000, engine_family="codex"))
    unidentified_cloud()
    assert ready(service) == []
    assert service.queue_metrics(OPERATOR)["overall"]["blocked"]["missing_placement_evidence"] == 2
    with pytest.raises(TaskServiceError, match="no longer ready"):
        claim(service, "a")
    placement(service, "a")
    assert ready(service)[0]["effective_placement"] == "local"
    assert "environment_id" in ready(service)[0]["placement_reason"]
    service.clock.now += 10
    unidentified_cloud()
    assert service.queue_metrics(OPERATOR)["overall"]["ready"][0]["oldest_ready_age_seconds"] == 10
    placement(service, "a", "cloud")
    assert ready(service)[0]["effective_placement"] == "cloud"


def test_route_quarantine_and_fresh_clearance(queue):
    projects, service = queue
    admit(projects, [task("a", repo="org/repo"), task("b", repo="org/repo"),
                     task("other", repo="org/other"), task("held", repo="org/repo"),
                     task("waiting", repo="org/repo", dependency_ids=["a"])])
    def probe(name, reference, environment="env"):
        service.record_placement(f"{PROJECT}/{name}", revision(service, name), PlacementEvidence(
            placement="local", eligible=True, reason="Green", reference=reference,
            expires_at=service.clock() + 1000, engine_family="codex", environment_id=environment))
    for name in ("a", "b", "other", "held", "waiting"):
        probe(name, "initial")
    assert "scheduled" in projects.projects_schedule_task(PROJECT, 3, "Produce held", "Waiting for input", hold=True)
    def fail(key, outcome):
        receipt = claim(service, "a", key)
        ref = evidence(service, receipt, submit(service, receipt), state="rework", outcome=outcome)
        return service.complete(WORKER, receipt["task_id"], receipt["claim_token"], key, "rework", ref, outcome)
    fail("infra1", "infrastructure_failure")
    fail("task", "task_failure")
    fail("infra2", "infrastructure_failure")
    fail("infra3", "infrastructure_failure")
    assert any(item["task_id"].endswith("/b") for item in ready(service))
    result = fail("infra4", "infrastructure_failure")
    assert result["completion"]["outcome"] == "infrastructure_failure"
    with pytest.raises(TaskServiceError) as refusal:
        claim(service, "b")
    assert refusal.value.code == "route_quarantined"
    assert [item["task_id"] for item in ready(service)] == [f"{PROJECT}/other"]
    blocked = service.queue_metrics(OPERATOR)["overall"]["blocked"]
    assert blocked["route_quarantined"] == 4
    assert blocked["hold"] == 1 and blocked["unmet_dependency"] == 1
    with pytest.raises(TaskServiceError, match="fresh"):
        service.route_clear(OPERATOR, f"{PROJECT}/b", "Verified recovery")
    service.clock.now += 1
    probe("b", "initial")
    with pytest.raises(TaskServiceError, match="fresh"):
        service.route_clear(OPERATOR, f"{PROJECT}/b", "Verified recovery")
    probe("b", "new-green")
    cleared = service.route_clear(OPERATOR, f"{PROJECT}/b", "Verified recovery")
    assert cleared["clearance"]["actor"] == "operator"
    assert cleared["clearance"]["reason"] == "Verified recovery"
    with Store(db=str(service.path)) as store:
        assert any(value["history"][-1] == cleared["clearance"] for _, value in store.items(prefix="task-route:")
                   if value.get("history"))
    assert claim(service, "b")["task_id"] == f"{PROJECT}/b"


def test_configurable_quarantine_threshold(queue):
    projects, original = queue
    service = ProjectTaskService(str(original.path), quarantine_threshold=1, clock=original.clock)
    admit(projects, [task("a", repo="org/repo"), task("b", repo="org/repo")])
    for name in ("a", "b"):
        service.record_placement(f"{PROJECT}/{name}", revision(service, name), PlacementEvidence(
            placement="local", eligible=True, reason="Green", reference="check",
            expires_at=service.clock() + 1000, engine_family="codex", environment_id="env"))
    receipt = claim(service, "a")
    ref = evidence(service, receipt, submit(service, receipt), state="rework", outcome="infrastructure_failure")
    service.complete(WORKER, receipt["task_id"], receipt["claim_token"], "done", "rework", ref,
                     "infrastructure_failure")
    with pytest.raises(TaskServiceError) as refusal:
        claim(service, "b")
    assert refusal.value.code == "route_quarantined"


def parallel(*operations):
    gate = Barrier(len(operations), timeout=10)

    def run(operation):
        gate.wait()
        try:
            return operation()
        except TaskServiceError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run, operations))


@pytest.mark.parametrize(("fields", "message"), [
    ({"dependency_ids": ["unknown"]}, "unknown dependency"),
    ({"dependency_ids": ["b"]}, "dependency cycle"),
    ({"tier": "other"}, "invalid tier"),
    ({"requested_placement": "other"}, "invalid requested_placement"),
])
def test_plan_validation_at_all_entry_points(queue, fields, message):
    projects, _ = queue
    bad = [task("a", **fields), task("b", dependency_ids=["a"])]
    args = (PROJECT, "Artifacts are visible", "2027-01-01T00:00:00Z", bad)
    verdict = projects.projects_review_plan(*args)
    assert not verdict["ok"] and message in str(verdict)
    assert message in projects.projects_promote(*args)
    admit(projects, [task("start"), task("end")])
    assert message in projects.projects_revise_plan(PROJECT, "Add tasks", bad, keep=[0, 1])


def test_review_digest_covers_fields_and_revision_preserves_legacy(queue):
    projects, service = queue
    first = {
        "text": "Legacy task", "acceptance_criteria": ["File exists"],
    }
    second = task(
        "second", "critical", dependency_ids=["@0"], requested_profile="read",
        requested_network=False, requested_placement="cloud", effective_placement="local",
        placement_reason="Preflight pending",
    )
    args = (PROJECT, "Artifacts are visible", "2027-01-01T00:00:00Z")
    assert projects.projects_review_plan(*args, [first, second])["ok"]
    assert "changed" in projects.projects_promote(*args, [first, {**second, "requested_network": True}])
    assert projects.projects_promote(*args, [first, second]).startswith("promoted")
    note = projects.projects_revise_plan(
        PROJECT, "Append work", ["Legacy append", task("third", dependency_ids=["second"])], keep=[0, 1]
    )
    assert "added 2" in note
    with Store(db=str(service.path)) as store:
        tasks = store.read(_board_key(PROJECT))["tasks"]
    assert {key: tasks[1][key] for key in second} == second
    assert tasks[3]["dependency_ids"] == ["second"]
    placement(service, "@0")
    assert ready(service)[0]["tier"] is None
    assert ready(service)[0]["dependencies"] == []


def test_dependencies_all_tiers_and_placement_revalidation(queue):
    projects, service = queue
    admit(projects, [
        task("a"), task("b", "writing"), task("c", "thinking", dependency_ids=["a", "b"],
                                             requested_placement="cloud"),
        task("d", "critical", requested_placement="cloud"),
    ])
    for identifier in ("a", "b", "c"):
        placement(service, identifier)
    placement(service, "d", "cloud", ttl=20)
    page = service.next_ready(OPERATOR, PROJECT, 1)
    assert page["tasks"][0]["tier"] == "basic"
    assert [x["tier"] for x in service.next_ready(OPERATOR, PROJECT, cursor=page["next_cursor"])["tasks"]] == [
        "writing", "critical",
    ]
    assert ready(service)[-1]["effective_placement"] == "cloud"
    service.clock.now += 21
    with pytest.raises(TaskServiceError, match="no longer ready"):
        claim(service, "d")
    placement(service, "d", eligible=False, reason="Local runner unavailable")
    assert [x["tier"] for x in ready(service)] == ["basic", "writing"]
    placement(service, "d")
    assert "stale" in ready(service)[-1]["placement_reason"]

    a = claim(service, "a", "a")
    complete(service, a, evidence(service, a, submit(service, a)))
    assert [x["tier"] for x in ready(service)] == ["writing", "critical"]
    b = claim(service, "b", "b")
    complete(service, b, evidence(service, b, submit(service, b), state="blocked"), state="blocked")
    assert [x["tier"] for x in ready(service)] == ["critical"]
    assert "reopened" in projects.projects_reopen_task(
        PROJECT, 1, "Produce b", "The unavailable test service has recovered."
    )
    b = claim(service, "b", "b-retry")
    complete(service, b, evidence(service, b, submit(service, b)), key="retry")
    result = ready(service)[0]
    assert result["tier"] == "thinking" and result["effective_placement"] == "local"
    assert "missing" in result["placement_reason"]
    placement(service, "c", "cloud", eligible=False, reason="Preflight rejected the environment")
    assert "Preflight rejected" in ready(service)[0]["placement_reason"]
    placement(service, "c", "cloud")
    assert ready(service)[0]["effective_placement"] == "cloud"


def test_race_claims_replay_and_reclaim_before_submit(queue):
    projects, service = queue
    admit(projects, [task("a"), task("b")])
    placement(service, "a")
    results = parallel(
        lambda: claim(service, "a", "one", WORKER),
        lambda: claim(service, "a", "two", REVIEWER),
    )
    assert results.count("conflict") == 1
    receipt = next(result for result in results if isinstance(result, dict))
    winner = WORKER if receipt["executor"] == WORKER.actor else REVIEWER
    assert claim(service, "a", receipt["idempotency_key"], winner) == receipt
    assert service.dispatch_status(OPERATOR, receipt["task_id"])["state"] == "intent"
    service.clock.now += 61
    service.reconcile_dispatch(OPERATOR, receipt["task_id"], receipt["attempt"], "absent")
    replacement = claim(service, "a", "replacement", REVIEWER)
    assert replacement["attempt"] == receipt["attempt"] + 1
    with pytest.raises(TaskServiceError, match="superseded"):
        service.begin_submit(winner, receipt["task_id"], receipt["claim_token"])
    with pytest.raises(TaskServiceError, match="superseded"):
        complete(service, receipt, "old-evidence", principal=winner)


def test_claim_replay_reports_dispatch_state_and_lease(queue):
    projects, service = queue
    admit(projects, [task("a"), task("b")])
    placement(service, "a")
    receipt = claim(service, "a")
    args = (WORKER, receipt["task_id"], receipt["revision"], receipt["idempotency_key"])
    assert service.claim(*args) == {"claim": receipt, "dispatch_state": "intent", "claim_active": True}
    service.begin_submit(WORKER, receipt["task_id"], receipt["claim_token"])
    assert service.claim(*args)["dispatch_state"] == "submitting"
    service.clock.now += 61
    assert service.claim(*args)["claim_active"] is False
    service.reconcile_dispatch(OPERATOR, receipt["task_id"], receipt["attempt"], "absent", evidence_ref="provider lookup")
    replay = service.claim(*args)
    assert replay == {"claim": receipt, "dispatch_state": "absent", "claim_active": False}
    claim(service, "a", "replacement", REVIEWER)
    assert service.claim(*args) == replay


@pytest.mark.parametrize("provider_received", [False, True])
def test_interruption_at_provider_submit_holds_until_reconciliation(queue, provider_received):
    projects, service = queue
    admit(projects, [task("a"), task("b")])
    placement(service, "a")
    receipt = claim(service, "a")
    identifier, token = receipt["task_id"], receipt["claim_token"]
    service.begin_submit(WORKER, identifier, token)
    # A fake provider tracks submissions independently of the service transaction.
    provider_jobs = {"dispatch": "provider-job"} if provider_received else {}
    service = ProjectTaskService(str(service.path), clock=service.clock)
    assert claim(service, "a") == receipt
    service.clock.now += 61
    service.reconcile_dispatch(OPERATOR, identifier, receipt["attempt"], "ambiguous", evidence_ref="lookup-timeout")
    with pytest.raises(TaskServiceError, match="no longer ready"):
        claim(service, "a", "replacement", REVIEWER)
    key = service.dispatch_status(OPERATOR, identifier)["idempotency_key"]
    if key in provider_jobs:
        service.reconcile_dispatch(
            OPERATOR, identifier, receipt["attempt"], "submitted", evidence_ref="provider-lookup", job_ref=provider_jobs[key]
        )
        assert service.dispatch_status(OPERATOR, identifier)["job_ref"] == "provider-job"
        with pytest.raises(TaskServiceError, match="conflicts"):
            service.reconcile_dispatch(OPERATOR, identifier, receipt["attempt"], "absent", evidence_ref="bad-lookup")
        assert len(provider_jobs) == 1
    else:
        service.reconcile_dispatch(OPERATOR, identifier, receipt["attempt"], "absent", evidence_ref="provider-lookup")
        assert claim(service, "a", "replacement", REVIEWER)["attempt"] == receipt["attempt"] + 1


def test_complete_replay_identity_and_expiry(queue):
    projects, service = queue
    admit(projects, [task("a"), task("b")])
    placement(service, "a")
    receipt = claim(service, "a", "first")
    job = submit(service, receipt)
    ref = evidence(service, receipt, job)
    with pytest.raises(TaskServiceError, match="different executor"):
        complete(service, receipt, ref, principal=REVIEWER)
    completions = parallel(
        lambda: complete(service, receipt, ref),
        lambda: service.reconcile_dispatch(
            OPERATOR, receipt["task_id"], receipt["attempt"], "terminated", job_ref=job, evidence_ref="job-state"
        ),
    )
    assert completions[0]["completion"]["state"] == "accepted"
    assert completions[1] in ("conflict", "stale")
    result = complete(service, receipt, ref)
    with pytest.raises(TaskServiceError, match="already been used"):
        complete(service, receipt, "different")
    service.clock.now += 61
    assert complete(service, receipt, ref) == result
    placement(service, "b")
    b = claim(service, "b")
    ref = evidence(service, b, submit(service, b))
    service.clock.now += 61
    expired = parallel(
        lambda: complete(service, b, ref),
        lambda: service.reconcile_dispatch(
            OPERATOR, b["task_id"], b["attempt"], "terminated", job_ref="job-dispatch", evidence_ref="terminal"
        ),
    )
    assert expired == ["stale", None]


def test_completion_reuses_the_latest_contract_acceptance(queue):
    projects, service = queue
    admit(projects, [task("a", repo="team/repo"), task("b", dependency_ids=["a"])])
    with Store(db=str(service.path)) as store:
        contract = contracts.open_task_contract(
            store, project_id=PROJECT, task_index=0, repo="team/repo", observable_result="File updated",
            acceptance_criteria=["File contains the new value"], required_checks=["pytest test_result.py"],
            risk="low", allowed_effects="Update the assigned branch",
            requires_review=False, review_waiver="The independent task reviews the resulting commit.",
        )
        placement(service, "a")
        placement(service, "b")
        receipt = claim(service, "a")
        job = submit(service, receipt)
        ref = evidence(service, receipt, job, verification_id=job)
        verification.claim_verification(store, contract_id=contract.contract_id, job_id=job)
        verification.start_running(store, job)
        with pytest.raises(TaskServiceError, match="latest accepted contract verification"):
            complete(service, receipt, ref)
        assert ready(service) == []
        verification.record_checks(
            store, job, checks=[
                verification.CheckResult(command="pytest test_result.py", exit_code=0, output_tail="Passed"),
            ], diff_summary="File updated",
        )
        verification.accept(store, job, reviewer=OPERATOR.actor, reason="The required check passed")
        assert complete(service, receipt, ref)["completion"]["verification_id"] == job
        assert ready(service)[0]["task_id"] == f"{PROJECT}/b"


@pytest.mark.parametrize(("writer_engine", "reviewer_engine"), [("codex", "claude_code"), ("claude_code", "codex")])
def test_review_exact_commit_and_opposite_engine(queue, writer_engine, reviewer_engine):
    projects, service = queue
    admit(projects, [
        task("write", "writing", repo="team/repo"),
        task("review", "thinking", review_of="write", dependency_ids=["write"]),
        task("deliver", "critical", dependency_ids=["review"]),
    ])
    placement(service, "write", engine=writer_engine)
    placement(service, "review", engine=writer_engine)
    placement(service, "deliver")
    writer = claim(service, "write", "write")
    complete(service, writer, evidence(service, writer, submit(service, writer)))
    same = claim(service, "review", "same", REVIEWER)
    job = submit(service, same, REVIEWER)
    ref = evidence(service, same, job, reviewed_sha=SHA, review_passed=True)
    with pytest.raises(TaskServiceError, match="opposite engine"):
        complete(service, same, ref, REVIEWER)
    service.clock.now += 61
    service.reconcile_dispatch(
        OPERATOR, same["task_id"], same["attempt"], "terminated", job_ref=job, evidence_ref="terminal-job"
    )
    placement(service, "review", engine=reviewer_engine)
    review = claim(service, "review", "opposite", REVIEWER)
    job = submit(service, review, REVIEWER)
    wrong = evidence(service, review, job, reviewed_sha=OTHER_SHA, review_passed=True)
    with pytest.raises(TaskServiceError, match="exact commit"):
        complete(service, review, wrong, REVIEWER)
    assert ready(service) == []
    ref = evidence(service, review, job, reference="latest-review", reviewed_sha=SHA, review_passed=True)
    with pytest.raises(TaskServiceError, match="current evidence"):
        complete(service, review, wrong, REVIEWER)
    complete(service, review, ref, REVIEWER)
    assert ready(service)[0]["task_id"] == f"{PROJECT}/deliver"


@pytest.mark.asyncio
async def test_mcp_lists_all_five_and_rejects_identity_in_arguments(queue):
    from lazytools.mcp_server import build_server, default_providers
    from mcp_test_utils import connected_client, protocol_field

    projects, service = queue
    admit(projects, [task("a"), task("b")])
    placement(service, "a")
    config = {"projects_store_db": str(service.path), "projects_task_principal": OPERATOR.model_dump(mode="json")}
    expected = {"tasks_next_ready", "tasks_queue_metrics", "tasks_claim", "tasks_complete", "tasks_route_clear",
                "approvals_pending", "approvals_decide"}
    providers = default_providers(["projects"], allow_write=True, data_source=config)
    async with connected_client(build_server(providers, read_only=False)) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert expected | {"projects_list"} <= tools
        result = await client.call_tool("tasks_next_ready", {"project_id": PROJECT})
        assert json.loads(result.content[0].text)["tasks"][0]["task_id"] == f"{PROJECT}/a"
        result = await client.call_tool("tasks_claim", {
            "task_id": f"{PROJECT}/a", "expected_revision": revision(service, "a"),
            "idempotency_key": "spoof", "actor": WORKER.actor,
        })
        assert protocol_field(result, "is_error", "isError") or not json.loads(result.content[0].text)["ok"]
        assert ready(service)
    async with connected_client(build_server(providers, read_only=True)) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert tools & expected == {"tasks_next_ready", "tasks_queue_metrics", "approvals_pending"}


def test_missing_persistent_store_refuses(queue):
    from lazytools.connectors.projects.task_tools import TaskTools

    tools = TaskTools(None, OPERATOR, allow_write=True)
    assert tools.tasks_next_ready(PROJECT)["error"]["code"] == "unavailable"
    assert tools.approvals_pending(PROJECT)["error"]["code"] == "unavailable"
