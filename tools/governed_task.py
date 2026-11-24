"""Run one coding task through the bridge and record it in the NC-7 trial ledger."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lazybridge import Store
from lazybridge.ext.approval import DEFAULT_PREFIX, ApprovalTicket
from lazybridge.ext.receipt import PublicationReceipt
from receipt_live_demo import (
    DemoError,
    bridge,
    claim,
    configure_state,
    finish_task,
    require,
    review_verdict,
    run_job,
    safe_path,
    step_trace,
    table_counts,
)

from lazytools.code_bridge import _cloud, _gh, _publication, _receipts, _registry, _store
from lazytools.code_bridge._lockfile import JobLock, LockHeld
from lazytools.connectors.projects import ProjectsTools
from lazytools.projects.plan_edit import _board_key
from lazytools.projects.task_service import ProjectTaskService, TaskPrincipal
from lazytools.projects.task_store import TaskServiceError, migrate_publication_receipts

DEFAULT_STATE = Path("D:/work/r2-live/trial/state")
DEFAULT_LEDGER = Path("D:/work/_wt/eo-decisions/design/new-course/trial/ledger.jsonl")
RUN_KEY = "governed-task:run"
TICKET_KEY = "governed-task:ticket"
VERIFIED_KEY = "governed-task:live-verified-receipt"
JOBS_KEY = "governed-task:jobs"


def db_options(stores: dict[str, Path]) -> list[str]:
    return ["--db", str(stores["bridge"]), "--registry-db", str(stores["registry"])]


def approval_command(stores: dict[str, Path], *args: str) -> str:
    words = ["lazytools-code-bridge", *args, *db_options(stores)]
    if os.name != "nt":
        return shlex.join(words)
    # Windows operators run the printed command in PowerShell.
    return " ".join(word if re.fullmatch(r"[\w./:\\-]+", word) else "'" + word.replace("'", "''") + "'"
                    for word in words)


def checkout_base(repo: Path, branch: str, base: str) -> tuple[str, str]:
    git = _cloud._git
    upstream = f"refs/remotes/origin/{base}"
    base_sha = git(repo, "rev-parse", "--verify", "--quiet", f"{upstream}^{{commit}}", check=False)
    if not base_sha:
        base_sha = git(repo, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}")
    head = _publication._branch_sha(repo, branch)
    if head is None:
        start = review_base = base_sha
        git(repo, "switch", "-c", branch, base_sha)
    else:
        start = head
        review_base = git(repo, "merge-base", base_sha, start)
        git(repo, "switch", "--no-guess", branch)
    require(stable_checkout(repo, branch) == start, "The branch changed during checkout")
    return start, review_base


def stable_checkout(repo: Path, branch: str) -> str:
    require(_cloud.current_branch(repo) == branch, f"Expected branch {branch} in {repo}")
    require(not _cloud._git(repo, "status", "--porcelain", "--untracked-files=all"),
            f"Checkout has uncommitted changes: {repo}")
    return _cloud._git(repo, "rev-parse", "HEAD")


def start_task(args: argparse.Namespace, stores: dict[str, Path], store: Store) -> None:
    repo, branch, base, project = args.repo, args.branch, args.base, args.project
    git = _cloud._git
    title = args.brief.splitlines()[0].lstrip("# ").removeprefix("Brief:").strip() or project
    writer_id, review_id = f"{project}/writer", f"{project}/review"
    review_engine = "claude" if args.engine == "codex" else "codex"
    model = args.model or ("gpt-6.2-sol" if args.engine == "codex" else None)

    step = step_trace(stores, table_counts(stores))
    issuer = _receipts.load_issuer()
    step(1, "Isolate state", f"{stores['projects'].parent} with three SQLite stores and issuer {issuer.fingerprint()}")
    start, review_base = checkout_base(repo, branch, base)
    projects = ProjectsTools(store_db=str(stores["projects"]), allow_write=True)
    require(" AS A DRAFT" in projects.projects_create(project, title, args.brief), f"Could not create {project}")
    common = {"repo": args.repository, "assigned_branch": branch, "requested_network": False}
    tasks = [
        {**common, "task_id": "writer", "text": title, "requested_profile": "edit-and-run",
         "acceptance_criteria": ["One local commit addresses the brief, and the checks required by the brief pass."]},
        {**common, "task_id": "review", "text": "Independently review the exact writer commit",
         "requested_profile": "read", "review_of": "writer", "dependency_ids": ["writer"],
         "acceptance_criteria": ["The reviewer accepts the writer's full commit SHA against the brief."]},
    ]
    plan = (project, f"A tested and independently reviewed commit for {title}",
            (datetime.now(UTC) + timedelta(days=1)).isoformat(), tasks)
    intake = projects.projects_review_plan(*plan)
    require(intake["ok"] and intake["recorded"], f"Plan review failed: {intake}")
    promoted = projects.projects_promote(*plan)
    require(promoted.startswith("promoted"), promoted)
    revisions = {task["task_id"]: task.get("revision", "0") for task in store.read(_board_key(project))["tasks"]}
    store.write(RUN_KEY, {"repo": str(repo), "branch": branch, "base": base, "title": title, "ledger": str(args.ledger)})
    step(2, "Create project and promote plan", f"{writer_id} and {review_id} (depends on writer)")

    # Leave time for the bridge's one-hour job timeout and for completion bookkeeping.
    service = ProjectTaskService(str(stores["projects"]), lease_seconds=3900)
    operator = TaskPrincipal(actor="governed-task", role="orchestrator", projects=(project,))
    writer = TaskPrincipal(actor="governed-writer", role="worker", projects=(project,))
    reviewer = TaskPrincipal(actor="governed-reviewer", role="worker", projects=(project,))
    writer_claim = claim(service, writer, writer_id, args.engine, revisions["writer"])
    print(f"Running {args.engine} writer on {branch} from {start}", flush=True)
    print(approval_command(stores, "pending"), flush=True)
    print(f"Replace TICKET with an ID from pending: {approval_command(stores, 'approve', 'TICKET')}", flush=True)
    writer_job, _ = run_job(
        repo, stores, args.engine, writer, writer_claim,
        f"Implement the brief on {branch}. Run the required checks and make exactly one local commit. "
        "Leave the checkout clean. Do not push or open a pull request. Use English and omit AI attribution.\n\n"
        f"{args.brief}",
        model=model, effort=args.effort,
    )
    candidate = stable_checkout(repo, branch)
    require(git(repo, "rev-list", "--parents", "-n", "1", candidate).split() == [candidate, start],
            "Writer must leave exactly one new commit on the assigned branch")
    evidence = finish_task(service, operator, writer, writer_claim, writer_job, candidate)
    step(3, "Claim, run and complete writer", f"{writer_id}, {args.engine} job {writer_job}, A={candidate}, {evidence}")

    review_claim = claim(service, reviewer, review_id, review_engine, revisions["review"])
    print(f"Running {review_engine} review of writer job {writer_job}", flush=True)
    review_job, result = run_job(
        repo, stores, review_engine, reviewer, review_claim,
        f"Review commit {candidate} against the writer brief below. Inspect git show {candidate} and "
        f"the proposed PR diff with git diff {review_base} {candidate}. Leave the checkout unchanged. "
        'Return one JSON object with verdict ("accepted" or "blocked"), '
        f'sha (the full commit SHA "{candidate}"), and reason (one sentence explaining the verdict).\n\n'
        f"Writer brief\n{args.brief}\n\nExtra review instructions\n{args.review_brief or 'None'}",
        review_of=writer_job,
    )
    require(stable_checkout(repo, branch) == candidate, "The checkout changed during review")
    accepted, reason = review_verdict(result, candidate)
    evidence = finish_task(service, operator, reviewer, review_claim, review_job, candidate,
                           review=True, accepted=accepted)
    store.write(JOBS_KEY, {"ids": [writer_job, review_job], "rejection": None if accepted else reason})
    verdict = "accepted" if accepted else "rejected"
    step(4, "Claim, run and complete independent review",
         f"{review_id}, {review_engine} job {review_job}, A={candidate}, {evidence}, {verdict}. {reason}")
    require(accepted, f"Review rejected {candidate}: {reason}")


def publish_task(args: argparse.Namespace, stores: dict[str, Path], store: Store, run: dict[str, Any]) -> None:
    task_id = f"{args.project}/writer"
    step = step_trace(stores, table_counts(stores))
    jobs = store.read(JOBS_KEY)
    require(jobs, "Run start through independent review before publishing")
    require(not jobs["rejection"], f"Review rejected publication: {jobs['rejection']}")
    command = ["publish", "--repo", run["repo"], "--task", task_id, "--branch", run["branch"],
               "--base", run["base"], *db_options(stores)]

    def publication_receipt(*options: str) -> PublicationReceipt:
        status, output = bridge([*command, *options])
        require(output, "Publication produced no receipt")
        outcome, receipt_id, _ = output.splitlines()[-1].split(maxsplit=2)
        receipt = PublicationReceipt.model_validate_json(_receipts.read_source(stores["projects"], receipt_id))
        require(receipt.outcome.status == outcome and status == int(outcome == "refused"), output)
        return receipt

    if not args.resume:
        receipt = publication_receipt("--request-only")
        ticket_id = receipt.approval.ticket_id
        if receipt.outcome.status == "published":
            print(f"Already published as {receipt.publication.pr_url}. Run publish --resume to verify the receipt.")
            return
        refusal = receipt.outcome.refusal_reason
        require(ticket_id and refusal in {"approval_not_decided", "publication_not_attempted"},
                f"Publication refused: {refusal} (receipt {receipt.receipt_id})")
        store.write(TICKET_KEY, ticket_id)
        target = receipt.approval.target
        step(5, "File publication ticket", f"{ticket_id} for {target.repository} {target.head_branch} "
             f"A={target.head_sha} into {run['base']}, receipt {receipt.receipt_id}")
        if refusal == "approval_not_decided":
            print(approval_command(stores, "approve", str(ticket_id)), flush=True)
        else:
            print(f"Ticket {ticket_id} is approved. Run publish --resume to publish.", flush=True)
        return

    ticket_id = store.read(TICKET_KEY)
    require(ticket_id, "Run publish to file a ticket before using --resume")
    receipt = publication_receipt("--ticket", ticket_id)
    require(receipt.outcome.status == "published", f"Publication refused: {receipt.outcome.refusal_reason} "
            f"(receipt {receipt.receipt_id})")
    step(6, "Publish approved head", f"{receipt.publication.pr_url}, receipt {receipt.receipt_id}")
    status, output = bridge(["verify-receipt", receipt.receipt_id, "--live"])
    require(status == 0 and output == "valid", f"Live verification failed for {receipt.receipt_id}: {output}")
    step(7, "Verify receipt against PR head", f"{receipt.receipt_id}, live valid")
    counts = table_counts(stores)
    replay = publication_receipt("--ticket", ticket_id)
    require(replay == receipt and table_counts(stores) == counts, "Replay changed the publication outcome")
    store.write(VERIFIED_KEY, receipt.receipt_id)
    step(8, "Replay publication", f"returned stored receipt {receipt.receipt_id}, PR {receipt.publication.pr_url}")


def trial_row(args: argparse.Namespace, stores: dict[str, Path], store: Store, task: str) -> dict[str, Any]:
    jobs = store.read(JOBS_KEY)
    receipt_id = store.read(VERIFIED_KEY)
    require(jobs and (receipt_id or jobs["rejection"]), "Finish publish --resume before recording the trial row")
    with Store(db=str(stores["bridge"])) as job_store:
        writer, reviewer = [_store.read_meta(job_store, job_id) for job_id in jobs["ids"]]
    require(writer and reviewer, "Writer or reviewer job metadata is missing")
    assert writer is not None and reviewer is not None
    seconds = sum(turn["ended_at"] - turn["started_at"] for job in (writer, reviewer) for turn in job["turns"])

    manual = args.manual
    task_ids = {f"{args.project}/writer", f"{args.project}/review"}
    with Store(db=str(stores["registry"])) as registry_store:
        for _, record in registry_store.items(prefix=DEFAULT_PREFIX):
            ticket = ApprovalTicket.model_validate(record)
            if ticket.task_id not in task_ids or not _registry.is_bridge_ticket(ticket) or ticket.decision is None:
                continue
            actor = ticket.decision.decider
            if actor not in {"routine-auto", "bridge-cleanup"} and not actor.startswith("profile:"):
                manual += 1

    wall_minutes = None
    if receipt_id:
        receipt = PublicationReceipt.model_validate_json(_receipts.read_source(stores["projects"], receipt_id))
        number = receipt.publication.pr_number
        require(number is not None, f"Receipt {receipt_id} has no pull request number")
        assert number is not None
        pr = _gh.pr_view(f"github.com/{receipt.repository.owner}/{receipt.repository.name}", number, timing=True)
        created_at = pr.get("createdAt")
        require(pr.get("url") == receipt.publication.pr_url and isinstance(created_at, str),
                f"Could not read PR creation time for receipt {receipt_id}")
        assert isinstance(created_at, str)
        pr_created = datetime.fromisoformat(created_at).timestamp()
        # GitHub timestamps have whole-second precision, unlike bridge job timestamps.
        wall_minutes = round(max(0, pr_created - writer["created_at"]) / 60, 4)

    return {
        "task": task, "flow": "unified", "wall_minutes": wall_minutes, "agent_minutes": round(seconds / 60, 4),
        "manual_interventions": manual, "stops_real": args.stop_real, "stops_false": args.stop_false,
        "reconstruct_seconds": args.reconstruct_seconds,
        "receipt": {"id": receipt_id, "verification": "live valid"} if receipt_id else None,
        "notes": args.notes,
    }


def append_ledger(args: argparse.Namespace, stores: dict[str, Path], store: Store, run: dict[str, Any]) -> None:
    path = safe_path(args.ledger or Path(run["ledger"]))
    task = f"{args.project}: {run['title']}"
    lock = JobLock(path.parent / ".governed-task-locks", path)
    lock.acquire(args.project)
    try:
        content = path.read_bytes() if path.exists() else b""
        for line in content.splitlines():
            if line.strip():
                row = json.loads(line)
                if (row.get("task"), row.get("flow")) == (task, "unified"):
                    print(f"Already recorded {task} in {path}")
                    return
        row = trial_row(args, stores, store, task)
        separator = b"\n" if content and not content.endswith(b"\n") else b""
        _receipts._write_atomic(path, content + separator + (json.dumps(row, ensure_ascii=False) + "\n").encode())
        print(f"Recorded {task} in {path}")
    finally:
        lock.release()


def at_file(value: str) -> str:
    if not value.startswith("@"):
        raise argparse.ArgumentTypeError("expected @file")
    try:
        content = Path(value[1:]).expanduser().read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not content:
        raise argparse.ArgumentTypeError("brief file is empty")
    return content


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return number


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--project", required=True, help="Unique trial task ID (lowercase letters, digits and hyphens)")
    shared.add_argument("--state-dir", type=Path, default=DEFAULT_STATE, help="Parent of the retained per-task state")
    subcommands = parser.add_subparsers(dest="command", required=True)
    start = subcommands.add_parser("start", parents=[shared], help="Run a writer and independent reviewer")
    start.add_argument("--repo", type=Path, required=True)
    start.add_argument("--branch", required=True)
    start.add_argument("--base", default="main")
    start.add_argument("--brief", type=at_file, required=True, metavar="@file")
    start.add_argument("--review-brief", type=at_file, metavar="@file")
    start.add_argument("--engine", choices=("codex", "claude"), default="codex")
    start.add_argument("--model", help="Writer model (Codex defaults to gpt-6.2-sol)")
    start.add_argument("--effort", default="high")
    start.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="Remember this trial JSONL file for ledger")
    publish = subcommands.add_parser("publish", parents=[shared], help="Request publication or resume after approval")
    publish.add_argument("--resume", action="store_true")
    ledger = subcommands.add_parser("ledger", parents=[shared], help="Record one trial row")
    ledger.add_argument("--ledger", type=Path, help="Override the trial JSONL file selected at start")
    ledger.add_argument("--manual", type=nonnegative, default=0, help="Additional manual interventions")
    ledger.add_argument("--stop-real", action="append", default=[], help="Real stop and its evidence link")
    ledger.add_argument("--stop-false", action="append", default=[], help="False stop and its evidence link")
    ledger.add_argument("--reconstruct-seconds", type=nonnegative, help="Manually timed record reconstruction")
    ledger.add_argument("--notes", default="")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", args.project):
        parser.error("--project must be 2 to 41 lowercase letters, digits or hyphens and start with a letter")
    return args


def execute(args: argparse.Namespace, stack: ExitStack) -> None:
    parent = safe_path(args.state_dir)
    state = safe_path(parent / f"task-{args.project}")
    stores = {name: state / f"{name}.sqlite" for name in ("projects", "bridge", "registry")}
    new = args.command == "start"
    if new:
        repo = args.repo = safe_path(args.repo)
        args.ledger = safe_path(args.ledger)
        git = _cloud._git
        require(_cloud.repo_root(repo).resolve() == repo, "--repo must name the root of a worktree")
        require(not state.is_relative_to(repo), "Place the state directory outside the checkout")
        lock = JobLock(parent / ".checkout-locks", repo)
        lock.acquire(args.project)
        stack.callback(lock.release)
        require(args.branch not in {args.base, "main", "master"}, "Choose a head branch distinct from the base")
        for branch in (args.branch, args.base):
            require(not branch.startswith("-"), f"Invalid branch name: {branch}")
            git(repo, "check-ref-format", f"refs/heads/{branch}")
        fetch = git(repo, "remote", "get-url", "--all", "origin").splitlines()
        push = git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()
        require(len(fetch) == len(push) == 1, "Expected one origin fetch URL and one origin push URL")
        args.repository = _publication._remote_repository(fetch[0])
        require(args.repository and args.repository == _publication._remote_repository(push[0]),
                "Origin must name one GitHub repository")
        require(not git(repo, "status", "--porcelain", "--untracked-files=all"), f"Checkout must be clean: {repo}")
        require(not state.exists(), f"Trial state already exists at {state}. Use a new project ID to start again.")
        state.mkdir(parents=True)
    else:
        require(all(path.is_file() for path in stores.values()), f"No task state at {state}. Run start first.")
    configure_state(state, stack)
    store = stack.enter_context(Store(db=str(stores["projects"])))
    if new:
        for name in ("bridge", "registry"):
            with Store(db=str(stores[name])):
                pass
        migrate_publication_receipts(stores["projects"])
        start_task(args, stores, store)
        return
    run = store.read(RUN_KEY)
    require(run, f"Task setup did not finish for {args.project}")
    if args.command == "publish":
        publish_task(args, stores, store, run)
    else:
        append_ledger(args, stores, store, run)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with ExitStack() as stack:
            execute(args, stack)
    except (DemoError, LockHeld, TaskServiceError, _cloud.CloudError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Governed task stopped: {_cloud.redact(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
