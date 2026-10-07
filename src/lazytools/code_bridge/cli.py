"""``lazytools-code-bridge`` -- an async CLI path to Codex / Claude Code writes.

See ``docs/code-bridge.md`` for the workflow this is built for: a Claude
Code session launches ``run`` via its Bash tool with ``run_in_background``
(so the launching session is never blocked on a long job and is notified
automatically when the process exits), polls ``pending`` for approval
tickets filed mid-job, relays them to the human in chat, and answers with
``approve``/``reject``; once notified, it reads ``result``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _jobs, _store
from lazytools.code_bridge._lockfile import LockHeld


def _read_task(raw: str) -> str:
    """``@path`` reads the task text from a file; anything else is the task itself."""
    if raw.startswith("@"):
        path = Path(raw[1:]).expanduser()
        return path.read_text(encoding="utf-8")
    return raw


def _db_path(args: argparse.Namespace) -> Path | None:
    return Path(args.db).expanduser() if getattr(args, "db", None) else None


def _add_db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=None,
        help="Store path (default: $LAZYTOOLS_CODE_BRIDGE_DB or ~/.lazytools/code-bridge.sqlite).",
    )


def _print_json(payload: Any) -> None:
    # One compact line, not pretty-printed: easier for a caller to parse
    # line-by-line (the `run` command's output in particular is job-id
    # line + one JSON line, and a multi-line payload would break that).
    print(json.dumps(payload, default=str))


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def _cmd_run(args: argparse.Namespace) -> int:
    task = _read_task(args.task)
    job_id_holder: dict[str, str] = {}

    def _announce(job_id: str) -> None:
        job_id_holder["job_id"] = job_id
        print(job_id, flush=True)  # first line: the caller can capture this immediately

    try:
        result = _jobs.run_job(
            engine_name=args.engine,
            cwd=args.cwd,
            task=task,
            session_name=args.session,
            model=args.model,
            effort=args.effort,
            root=args.root,
            db_path=_db_path(args),
            on_job_id=_announce,
        )
    except LockHeld as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        _print_json(
            {
                "job_id": result.job_id,
                "status": result.status,
                "result_path": str(result.result_path) if result.result_path else None,
                "error": result.error,
            }
        )
    else:
        preview = (result.text or result.error or "").strip().replace("\n", " ")[:200]
        print(f"[{result.status}] job {result.job_id}")
        print(f"result: {result.result_path}" if result.result_path else "result: (could not be persisted to a file)")
        if preview:
            print(preview)
    return 0 if result.status == "done" else 1


# --------------------------------------------------------------------------- #
# jobs / status / result
# --------------------------------------------------------------------------- #


def _job_line(job: dict[str, Any]) -> str:
    short_id = str(job.get("job_id", "?"))[:8]
    engine = job.get("engine", job.get("kind", "?"))
    status = job.get("status", "?")
    cwd = job.get("cwd", "?")
    objective = str(job.get("objective") or "")[:80]
    return f"{short_id}  {engine:<6}  {status:<16}  {cwd}  {objective}"


def _cmd_jobs(args: argparse.Namespace) -> int:
    store = _store.build_store(_db_path(args))
    rows = _jobs.list_jobs(store, all_jobs=args.all)
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("no jobs yet" if args.all else "no active jobs (pass --all for history)")
        return 0
    for row in rows:
        print(_job_line(row))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store = _store.build_store(_db_path(args))
    registry = _store.build_job_registry(store)
    job = _jobs.find_job(store, registry, args.job_id)
    if job is None:
        print(f"no job found matching {args.job_id!r}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(job)
    else:
        print(_job_line(job))
    return 0


def _cmd_result(args: argparse.Namespace) -> int:
    store = _store.build_store(_db_path(args))
    registry = _store.build_job_registry(store)
    job = _jobs.find_job(store, registry, args.job_id)
    if job is None:
        print(f"no job found matching {args.job_id!r}", file=sys.stderr)
        return 1
    status = job.get("status", "?")
    # Same exit code either way -- a caller scripting on exit status must
    # not see a failed job reported as success merely because it asked for
    # --json. Found by Codex review (it previously always returned 0 here).
    exit_code = 1 if status in ("failed", "interrupted") else 0
    if args.json:
        _print_json(job)
        return exit_code
    print(f"[{status}] job {job.get('job_id')}")
    if job.get("result"):
        print(job["result"])
    elif job.get("error"):
        print(f"error: {job['error']}", file=sys.stderr)
    else:
        print("(no result yet)")
    return exit_code


# --------------------------------------------------------------------------- #
# pending / approve / reject
# --------------------------------------------------------------------------- #


def _request_detail(prompt: str) -> dict[str, str]:
    """The command and reason a Codex escalation actually carries.

    A Codex sandbox escalation reaches TieredGate as the opaque tool name
    "codex-shell"; the real command line and Codex's own reason sit in the
    JSON on the prompt's "arguments:" line. Without them a person is asked to
    approve "codex-shell" blind. Empty when the prompt has no such payload."""
    for line in prompt.splitlines():
        line = line.strip()
        if not line.startswith("arguments:"):
            continue
        try:
            payload = json.loads(line[len("arguments:") :])
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        actions = payload.get("commandActions") or []
        commands = [str(a["command"]) for a in actions if isinstance(a, dict) and a.get("command")]
        command = " && ".join(commands) or str(payload.get("command") or "")
        detail = {"command": command, "reason": str(payload.get("reason") or "")}
        return {k: v for k, v in detail.items() if v}
    return {}


def _cmd_pending(args: argparse.Namespace) -> int:
    from lazybridge.ext.approval import ticket_gist

    store = _store.build_store(_db_path(args))
    queue = _store.build_approval_queue(store)
    tickets = queue.list_pending_tickets()
    if args.json:
        _print_json(
            [
                {
                    "approval_id": t.approval_id,
                    "job_id": t.task_id,
                    "gist": ticket_gist(t.prompt),
                    **_request_detail(t.prompt),
                    "kind": t.kind,
                    "created_at": t.created_at.isoformat(),
                    "expires_at": t.expires_at.isoformat(),
                }
                for t in tickets
            ]
        )
        return 0
    if not tickets:
        print("no pending approvals")
        return 0
    for t in tickets:
        print(f"{t.approval_id}  job={t.task_id[:8]}  {ticket_gist(t.prompt, max_len=120)}")
        for key, value in _request_detail(t.prompt).items():
            print(f"  {key}: {value}")
        print(f"  created {t.created_at.isoformat()}  expires {t.expires_at.isoformat()}")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    store = _store.build_store(_db_path(args))
    queue = _store.build_approval_queue(store)
    ok = queue.approve_ticket(args.ticket_id, actor=args.actor, channel="cli")
    if not ok:
        print(f"could not approve {args.ticket_id!r}: not pending, already resolved, or expired", file=sys.stderr)
        return 1
    print(f"approved {args.ticket_id}")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    store = _store.build_store(_db_path(args))
    queue = _store.build_approval_queue(store)
    ok = queue.reject_ticket(args.ticket_id, actor=args.actor, channel="cli", reason=args.reason)
    if not ok:
        print(f"could not reject {args.ticket_id!r}: not pending, already resolved, or expired", file=sys.stderr)
        return 1
    print(f"rejected {args.ticket_id}")
    return 0


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazytools-code-bridge",
        description="Asynchronous delegation of coding work to Codex / Claude Code, with human approvals over a durable ticket queue.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one coding-engine job to completion in this process.")
    run_p.add_argument("--engine", choices=list(_jobs.ENGINES), required=True)
    run_p.add_argument("--cwd", required=True, help="Repository (or subdirectory) to work in.")
    run_p.add_argument("--task", required=True, help="Task text, or @path/to/file.")
    run_p.add_argument("--session", default=None, help="Durable session alias to create/resume.")
    run_p.add_argument("--model", default=None)
    run_p.add_argument("--effort", default=None)
    run_p.add_argument(
        "--root", default=None, help="Confinement root for --cwd (default: $LAZYTOOLS_CODE_ROOT or cwd)."
    )
    run_p.add_argument("--json", action="store_true")
    _add_db_option(run_p)
    run_p.set_defaults(func=_cmd_run)

    jobs_p = sub.add_parser("jobs", help="List jobs.")
    jobs_p.add_argument("--all", action="store_true", help="Include finished jobs, not just active ones.")
    jobs_p.add_argument("--json", action="store_true")
    _add_db_option(jobs_p)
    jobs_p.set_defaults(func=_cmd_jobs)

    status_p = sub.add_parser("status", help="Show one job's current status.")
    status_p.add_argument("job_id")
    status_p.add_argument("--json", action="store_true")
    _add_db_option(status_p)
    status_p.set_defaults(func=_cmd_status)

    result_p = sub.add_parser("result", help="Show one job's result or error.")
    result_p.add_argument("job_id")
    result_p.add_argument("--json", action="store_true")
    _add_db_option(result_p)
    result_p.set_defaults(func=_cmd_result)

    pending_p = sub.add_parser("pending", help="List open approval tickets.")
    pending_p.add_argument("--json", action="store_true")
    _add_db_option(pending_p)
    pending_p.set_defaults(func=_cmd_pending)

    approve_p = sub.add_parser("approve", help="Approve a pending ticket.")
    approve_p.add_argument("ticket_id")
    approve_p.add_argument("--actor", default="operator")
    _add_db_option(approve_p)
    approve_p.set_defaults(func=_cmd_approve)

    reject_p = sub.add_parser("reject", help="Reject a pending ticket.")
    reject_p.add_argument("ticket_id")
    reject_p.add_argument("--reason", required=True)
    reject_p.add_argument("--actor", default="operator")
    _add_db_option(reject_p)
    reject_p.set_defaults(func=_cmd_reject)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


__all__ = ["build_parser", "main"]
