"""Run the Projects-to-publication flow from a LazyTools source checkout.

Live mode creates a local demo branch in a clean checkout. The publish scenario
uses --operator to approve the printed target and opens a draft PR on origin.
Dry-run mode uses a private local clone and the bridge's existing test fakes.
Both modes retain the state directory so the receipts remain inspectable.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from lazybridge import Store
from lazybridge.ext.receipt import PublicationReceipt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lazytools.code_bridge import _cloud, _publication, _receipts, _registry, _routing, _task, cli
from lazytools.connectors.projects import ProjectsTools
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_service import (
    CompletionEvidence,
    PlacementEvidence,
    ProjectTaskService,
    ResultState,
    TaskPrincipal,
)
from lazytools.projects.task_store import TaskServiceError, migrate_publication_receipts
from lazytools.routing.catalogue import DEFAULT_PATH

SOURCE = "receipt_demo_sub.py"
TEST = "test_receipt_demo_sub.py"
Counts = dict[str, int]


class DemoError(Exception):
    pass


def require(condition: object, message: str) -> None:
    if not condition:
        raise DemoError(message)


def safe_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    reserved = [Path.home() / ".lazytools"]
    if os.name == "nt":
        reserved.extend((Path("C:/ProgramData"), Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))))
    require(not any(path.is_relative_to(root.resolve()) for root in reserved), f"Reserved path: {path}")
    return path


def isolated_state(parent: Path | None, stack: ExitStack) -> Path:
    parent = safe_path(parent or Path(tempfile.gettempdir()))
    parent.mkdir(parents=True, exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix="receipt-demo-", dir=parent))
    configure_state(state, stack)
    return state


def configure_state(state: Path, stack: ExitStack) -> None:
    temp = state / "tmp"
    temp.mkdir(exist_ok=True)
    config = state / "code-bridge.toml"
    config.write_text(f"[concurrency]\nslot_dir = {json.dumps((state / 'slots').as_posix())}\n", encoding="utf-8")
    stack.enter_context(patch.object(tempfile, "tempdir", str(temp)))
    stack.enter_context(patch.dict(os.environ, {
        "LAZYTOOLS_PROJECTS_STORE_DB": str(state / "projects.sqlite"),
        "LAZYTOOLS_CODE_BRIDGE_DB": str(state / "bridge.sqlite"),
        "LAZYTOOLS_RECEIPT_ISSUER_KEY": str(state / "issuer.pem"),
        "LAZYTOOLS_CODE_BRIDGE_CONFIG": str(config),
        "LAZYTOOLS_QUOTA_CACHE": str(state / "quota-cache.json"),
        "LAZYBRIDGE_SESSIONS_FILE": str(state / "sessions.json"),
        "TEMP": str(temp),
        "TMP": str(temp),
        "TMPDIR": str(temp),
    }))
    for name in ("LAZYTOOLS_CODE_BRIDGE_PROFILES", _task.MCP_POLICY_DIGEST_ENV):
        os.environ.pop(name, None)


def table_counts(stores: dict[str, Path]) -> Counts:
    counts = {}
    for name, path in stores.items():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            tables = db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            for (table,) in tables:
                if not table.startswith("sqlite_"):
                    quoted = table.replace('"', '""')
                    counts[f"{name}.{table}"] = db.execute(f'SELECT count(*) FROM "{quoted}"').fetchone()[0]
    return counts


def step_trace(stores: dict[str, Path], previous: Counts) -> Callable[[int, str, str], None]:
    def step(number: int, action: str, result: str) -> None:
        nonlocal previous
        current = table_counts(stores)
        delta = ", ".join(f"{key} {value - previous.get(key, 0):+d}" for key, value in current.items()
                          if value != previous.get(key, 0))
        print(f"STEP {number}: {action} -> {result} [rows added: {delta or '0'}]", flush=True)
        previous = current

    return step


def bridge_output(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        status = cli.main(argv)
    if (detail := stderr.getvalue().strip()) and (status != 0 or argv[0] == "publish"):
        print(detail, file=sys.stderr)
    return status, stdout.getvalue().strip(), detail


def bridge(argv: list[str]) -> tuple[int, str]:
    status, output, _ = bridge_output(argv)
    return status, output


def claim(
    service: ProjectTaskService, principal: TaskPrincipal, task_id: str, engine: str, revision: str
) -> dict[str, Any]:
    service.record_placement(task_id, revision, PlacementEvidence(
        placement="local", eligible=True, reason="Local checkout and engine selected",
        reference=f"local-{task_id}", expires_at=service.clock() + service.lease_seconds + 60,
        engine_family=_routing.provider(engine),
    ))
    return cast(dict[str, Any], service.claim(principal, task_id, revision, f"local-dispatch-{revision}")["claim"])


def run_job(
    repo: Path, stores: dict[str, Path], engine: str, principal: TaskPrincipal, lease: dict[str, Any],
    prompt: str, *, review_of: str | None = None, model: str | None = None, effort: str | None = None,
    session: str | None = None,
) -> tuple[str, str]:
    argv = [
        "run", "--cwd", str(repo), "--root", str(repo), "--where", "local",
        "--engine", engine, "--launch-mode", "legacy", "--network", "off",
        "--no-freshness-check", "--no-brief-check", "--no-routine-auto", "--max-continuations", "0",
        "--task-id", lease["task_id"], "--claim-token", lease["claim_token"], "--task", prompt, "--json",
        "--db", str(stores["bridge"]), "--registry-db", str(stores["registry"]),
    ]
    if review_of:
        argv += ["--review-of", review_of, "--tier", "basic", "--tiers", str(DEFAULT_PATH)]
    if session is not None:
        argv += ["--session", session]
    if model is not None:
        argv += ["--model", model]
    if effort is not None:
        argv += ["--effort", effort]
    with patch.dict(os.environ, {_task.PRINCIPAL_ENV: principal.model_dump_json()}):
        status, output, detail = bridge_output(argv)
    if status != 0 or not output:
        error = None
        if output:
            try:
                error = json.loads(output.splitlines()[-1]).get("error")
            except (ValueError, AttributeError):
                pass
        raise DemoError(f"{engine} job failed: {error or detail or output or 'bridge returned no output'}")
    result = json.loads(output.splitlines()[-1])
    require(result.get("status") == "done", f"Unexpected job result: {result}")
    job_id = result["job_id"]
    status, output = bridge(["result", job_id, "--json", "--db", str(stores["bridge"])])
    require(status == 0 and output, f"Could not read result for {job_id}")
    return job_id, json.loads(output)["result"]


def finish_task(
    service: ProjectTaskService, operator: TaskPrincipal, worker: TaskPrincipal, lease: dict[str, Any],
    job: str, sha: str, *, review: bool = False, accepted: bool = True,
) -> str:
    state: ResultState = "accepted" if accepted else "blocked"
    reference = f"job-{job}-result"
    evidence = CompletionEvidence(
        reference=reference, job_ref=job, executor=worker.actor, engine_family=lease["engine_family"],
        state=state, artifact_ref=sha, reviewed_sha=sha if review else None,
        review_passed=review and accepted, review_independence="opposite-engine" if review else None,
    )
    service.record_completion_evidence(operator, lease["task_id"], lease["attempt"], evidence)
    service.complete(worker, lease["task_id"], lease["claim_token"], f"local-complete-{lease['attempt']}", state, reference)
    return reference


def review_verdict(result: str, sha: str) -> tuple[bool, str]:
    result = result.strip()
    if result.startswith("```json\n") and result.endswith("\n```"):
        result = result[len("```json\n"):-len("\n```")]
    try:
        review = json.loads(result)
    except ValueError:
        return False, f"Reviewer returned no structured verdict: {result}"
    if (
        not isinstance(review, dict) or review.get("sha") != sha
        or review.get("verdict") not in ("accepted", "blocked")
        or not isinstance(review.get("reason"), str) or not review["reason"].strip()
    ):
        return False, f"Reviewer returned no valid verdict for {sha}: {result}"
    return review["verdict"] == "accepted", " ".join(review["reason"].split())


def run_demo(args: argparse.Namespace, stack: ExitStack) -> None:
    repo = safe_path(args.repo)
    git = _cloud._git
    require(_cloud.repo_root(repo).resolve() == repo, "--repo must be a repository root")
    git(repo, "check-ref-format", f"refs/heads/{args.base}")
    require(not args.base.startswith("-"), "--base must be a branch name")
    parent = safe_path(args.state_dir or Path(tempfile.gettempdir()))
    require(args.dry_run or not parent.is_relative_to(repo), "Place the state directory outside the live checkout")
    state = isolated_state(parent, stack)
    stores = {name: state / f"{name}.sqlite" for name in ("projects", "bridge", "registry")}
    for path in stores.values():
        with Store(db=str(path)):
            pass
    migrate_publication_receipts(stores["projects"])
    issuer = _receipts.load_issuer()
    before = table_counts(stores)
    step = step_trace(stores, before)

    mode = "dry run" if args.dry_run else "live"
    step(1, "Isolate state", f"{mode} in {state} with three SQLite stores and issuer {issuer.fingerprint()}")
    receipts: dict[str, PublicationReceipt] = {}
    verified: dict[str, str] = {}
    last_receipt: PublicationReceipt | None = None
    writer_job = ""
    identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    project = f"receipt-demo-{identifier.lower()}"
    branch = f"demo/{identifier}"
    writer_id = f"{project}/writer"
    options = ["--db", str(stores["bridge"]), "--registry-db", str(stores["registry"])]
    service = ProjectTaskService(str(stores["projects"]), lease_seconds=3900)  # covers a 3600 s agent turn
    operator = TaskPrincipal(actor=args.operator, role="orchestrator", projects=(project,))
    writer = TaskPrincipal(actor="receipt-demo-writer", role="worker", projects=(project,))
    reviewer = TaskPrincipal(actor="receipt-demo-reviewer", role="worker", projects=(project,))
    review_engine = "claude" if args.engine == "codex" else "codex"

    def publish(*extra: str) -> PublicationReceipt:
        nonlocal last_receipt
        status, output = bridge([
            "publish", "--repo", str(repo), "--task", writer_id, "--branch", branch,
            "--base", args.base, *options, *extra,
        ])
        require(output, "Publication produced no receipt")
        outcome, receipt_id, detail = output.splitlines()[-1].split(maxsplit=2)
        require(outcome in ("refused", "published") and status == int(outcome == "refused"), output)
        raw = _receipts.read_source(stores["projects"], receipt_id)
        receipt = PublicationReceipt.model_validate_json(raw)
        receipts[receipt_id] = last_receipt = receipt
        require(receipt.outcome.status == outcome, f"Receipt outcome differs from CLI output: {detail}")
        return receipt

    def verify(receipt: PublicationReceipt, *, live: bool = False) -> str:
        command = ["verify-receipt", receipt.receipt_id]
        if live:
            command.append("--live")
        status, output = bridge(command)
        result = f"{'live' if live else 'signature'} {output or 'verification unavailable'}"
        verified[receipt.receipt_id] = result
        require(status == 0 and output == "valid", f"Verification failed for {receipt.receipt_id}: {result}")
        return result

    try:
        if args.dry_run:
            sys.path.insert(0, str(ROOT / "tests"))
            stack.callback(sys.path.remove, str(ROOT / "tests"))
            fake = importlib.import_module("_receipt_demo_fakes")
            repo = fake.install(stack, state, repo, args.base, SOURCE, TEST)
        fetch = git(repo, "remote", "get-url", "--all", "origin").splitlines()
        push = git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()
        require(len(fetch) == len(push) == 1, "Expected one origin fetch URL and one origin push URL")
        repository = _publication._remote_repository(fetch[0])
        require(repository and repository == _publication._remote_repository(push[0]), "Origin must name one GitHub repository")
        require(not git(repo, "status", "--porcelain", "--untracked-files=all"), f"Checkout must be clean: {repo}")
        require(not any(os.path.lexists(repo / name) for name in (SOURCE, TEST)), "Demo file names already exist")
        start = git(repo, "rev-parse", "HEAD")
        git(repo, "var", "GIT_AUTHOR_IDENT")
        git(repo, "var", "GIT_COMMITTER_IDENT")
        projects = ProjectsTools(store_db=str(stores["projects"]), allow_write=True)
        require(" AS A DRAFT" in projects.projects_create(project, "Publication receipt demo", "Publish an independently reviewed subtraction function"),
                f"Could not create {project}")
        tasks = [
            {"task_id": "writer", "text": "Add the subtraction function and its test", "repo": repository,
             "assigned_branch": branch, "requested_profile": "edit", "requested_network": False,
             "acceptance_criteria": ["The subtraction unit test passes on the candidate commit"]},
            {"task_id": "review", "text": "Review the exact writer commit", "repo": repository,
             "assigned_branch": branch, "review_of": "writer", "dependency_ids": ["writer"],
             "requested_profile": "read", "requested_network": False,
             "acceptance_criteria": ["The independent reviewer accepts the writer's full commit SHA"]},
        ]
        plan = (project, "A tested commit with an independent review and a signed publication receipt",
                (datetime.now(UTC) + timedelta(days=1)).isoformat(), tasks)
        intake = projects.projects_review_plan(*plan)
        require(intake["ok"] and intake["recorded"], f"Plan review failed: {intake}")
        promoted = projects.projects_promote(*plan)
        require(promoted.startswith("promoted"), promoted)
        step(2, "Create project and promote plan", f"{project}, tasks {writer_id} and {project}/review (depends on writer)")
        with Store(db=str(stores["projects"])) as store:
            revisions = {task["task_id"]: task.get("revision", "0") for task in store.read(_board_key(project))["tasks"]}

        git(repo, "switch", "-c", branch)
        writer_claim = claim(service, writer, writer_id, args.engine, revisions["writer"])
        print(f"Running {args.engine} writer on {branch} from {start}", flush=True)
        writer_job, _ = run_job(
            repo, stores, args.engine, writer, writer_claim,
            f"Create exactly two new files. In {SOURCE}, define sub(a, b) returning a - b. "
            f"In {TEST}, use unittest to test the function, including 2 minus 5. "
            "Leave both files unstaged. The demo driver will run the test and create the commit.",
        )
        require(git(repo, "rev-parse", "HEAD") == start and _cloud.current_branch(repo) == branch,
                "Writer changed the branch or committed before validation")
        changes = set(git(repo, "status", "--porcelain", "--untracked-files=all").splitlines())
        require(changes == {f"?? {SOURCE}", f"?? {TEST}"} and all(
            (repo / name).is_file() and not (repo / name).is_symlink() for name in (SOURCE, TEST)
        ), f"Writer must produce exactly the two new demo files: {sorted(changes)}")
        subprocess.run([sys.executable, "-B", "-m", "unittest", Path(TEST).stem], cwd=repo, check=True)
        git(repo, "add", "--", SOURCE, TEST)
        git(repo, "commit", "-m", "Add subtraction example and unit test")
        candidate = git(repo, "rev-parse", "HEAD")
        evidence = finish_task(service, operator, writer, writer_claim, writer_job, candidate)
        step(3, "Claim, run and complete writer",
             f"{writer_id} attempt {writer_claim['attempt']}, {args.engine} job {writer_job}, A={candidate}, board evidence {evidence}")

        review_id = f"{project}/review"
        review_claim = claim(service, reviewer, review_id, review_engine, revisions["review"])
        print(f"Running {review_engine} review of writer job {writer_job}", flush=True)
        review_job, result = run_job(
            repo, stores, review_engine, reviewer, review_claim,
            f"Review commit {candidate} using git show {candidate}. Check that it only adds {SOURCE} and {TEST}, "
            "implements subtraction correctly, and tests the negative result of 2 minus 5. "
            "The demo driver ran the unit test successfully. Return one JSON object with keys "
            f'verdict ("accepted" or "blocked"), sha (the full commit SHA "{candidate}"), '
            "and reason (one sentence explaining the verdict).",
            review_of=writer_job,
        )
        require(git(repo, "rev-parse", "HEAD") == candidate and _cloud.current_branch(repo) == branch
                and not git(repo, "status", "--porcelain"), "The checkout changed during review")
        accepted, reason = review_verdict(result, candidate)
        evidence = finish_task(service, operator, reviewer, review_claim, review_job, candidate, review=True, accepted=accepted)
        step(4, "Claim, run and complete independent review",
             f"{review_id} attempt {review_claim['attempt']}, {review_engine} job {review_job}, A={candidate}, "
             f"board evidence {evidence}, {'accepted' if accepted else 'blocked'}. {reason}")
        require(accepted, "Reviewer did not accept the candidate")

        if args.scenario == "drift":
            git(repo, "commit", "--allow-empty", "-m", "Advance demo branch after review")
            head = git(repo, "rev-parse", "HEAD")
            receipt = publish()
            require(receipt.outcome.refusal_reason == "head_changed_after_review" and receipt.publication.head_sha == head
                    and receipt.reviewer.reviewed_sha == candidate and receipt.approval.ticket_id is None
                    and receipt.publication.pr_url is None, "Expected a drift refusal before approval or push")
            verification = verify(receipt)
            step(5, "Advance branch and publish",
                 f"head_changed_after_review, reviewed A={candidate}, observed B={head}, no push, receipt {receipt.receipt_id}, {verification}")
        else:
            pending = publish()
            ticket_id = pending.approval.ticket_id
            require(pending.outcome.refusal_reason == "approval_not_decided" and ticket_id, "Expected a pending publication ticket")
            pending_verification = verify(pending)
            with Store(db=str(stores["bridge"])) as job_store:
                registry = _registry.Registry(job_store, stores["registry"])
                ticket = registry.inspect(cast(str, ticket_id))
                require(ticket is not None and ticket.request is not None and ticket.status == "pending", "Pending ticket is missing")
                assert ticket is not None and ticket.request is not None
                request = ticket.request
                target = request.target
                require((request.task_id, request.action, target.repo, target.ref, target.sha, request.payload.get("base_branch"))
                        == (writer_id, "open_draft_pr", repository, branch, candidate, args.base), "Unexpected approval target")
                step(6, "File publication ticket",
                     f"{ticket.approval_id} pending for {target.repo} {target.ref} A={target.sha} into {args.base}, "
                     f"receipt {pending.receipt_id}, {pending_verification}")
                require(registry.decide(ticket, "approved", operator.actor, "Approve the displayed demo target"),
                        f"Could not approve ticket {ticket.approval_id}")
                step(6, "Approve publication ticket", f"{ticket.approval_id} approved by {operator.actor}, registry row updated")
            receipt = publish("--ticket", cast(str, ticket_id))
            require(receipt.outcome.status == "published" and receipt.publication.head_sha == candidate
                    and receipt.publication.pr_url and receipt.approval.decided_by == operator.actor,
                    f"Expected a draft PR at A, got {receipt.outcome}")
            step(6, "Publish approved head",
                 f"draft PR {receipt.publication.pr_url} at A={candidate}, receipt {receipt.receipt_id}")
            step(6, "Verify receipt against PR head", f"{receipt.receipt_id}, {verify(receipt, live=True)}")
            published_counts = table_counts(stores)
            replay = publish()
            require(replay == receipt and table_counts(stores) == published_counts, "Replay changed the publication outcome")
            step(6, "Replay publication", f"returned stored receipt {replay.receipt_id}, no second PR")
    finally:
        pr_url = last_receipt.publication.pr_url if last_receipt else None
        receipt_id = last_receipt.receipt_id if last_receipt else None
        step(7, "Summarize run", f"PR {pr_url or 'none'}, receipt {receipt_id or 'none'}")
        for key, count in table_counts(stores).items():
            initial = before.get(key, 0)
            print(f"  {key}: {initial} -> {count} ({count - initial:+d} rows)")
        for receipt_id in receipts:
            path = state / "results" / writer_job / "publication" / receipt_id / "receipt.json"
            print(f"  {receipt_id}: {verified.get(receipt_id, 'verification not run')}, {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Local repository root")
    parser.add_argument("--state-dir", type=Path, help="Parent directory for a fresh, retained run directory")
    parser.add_argument("--engine", choices=("codex", "claude"), default="codex", help="Writer engine")
    parser.add_argument("--operator", help="Actor to record on the publication approval")
    parser.add_argument("--base", default="main", help="Base branch for the draft PR (default: main)")
    parser.add_argument("--scenario", choices=("drift", "publish"), default="publish")
    parser.add_argument("--dry-run", action="store_true", help="Use fake agents and GitHub, with a private local checkout")
    args = parser.parse_args()
    default_actor = "receipt-demo-operator" if args.dry_run or args.scenario == "drift" else ""
    args.operator = (args.operator or default_actor).strip()
    if not re.fullmatch(r"[A-Za-z0-9_@][A-Za-z0-9_.@:/-]*", args.operator) or re.match(r"^[A-Za-z]:/", args.operator):
        parser.error("--operator must name the operator actor (for example, your GitHub login)")
    try:
        with ExitStack() as stack:
            run_demo(args, stack)
    except (DemoError, TaskServiceError, _cloud.CloudError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Demo stopped: {_cloud.redact(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
