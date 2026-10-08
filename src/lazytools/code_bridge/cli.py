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
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _jobs, _routing, _store
from lazytools.code_bridge._lockfile import LockHeld
from lazytools.routing.policy import DEFAULT_POLICY


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


#: Statuses a job record can end in. A record still "running" or
#: "awaiting_approval" whose process is gone is reported as died by `wait`.
_TERMINAL = ("done", "failed", "interrupted")

# Windows process-creation flags for a child that must outlive its launcher:
# no console tie to the parent, its own process group (so a Ctrl+C or a
# console close aimed at the launcher does not reach it), and out of the
# launcher's job object when that object allows it -- a job object is what
# lets a supervising process (e.g. a Claude Code background shell being
# reaped) take its whole tree down with it.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _child_argv(args: argparse.Namespace, job_id: str) -> list[str]:
    argv = [sys.executable, "-m", "lazytools.code_bridge", "run", "--engine", args.engine, "--cwd", args.cwd]
    argv += ["--task", args.task, "--job-id", job_id]
    for flag, value in (("--session", args.session), ("--model", args.model), ("--effort", args.effort)):
        if value:
            argv += [flag, value]
    if args.root:
        argv += ["--root", args.root]
    if args.db:
        argv += ["--db", args.db]
    if getattr(args, "routing", None) is not None:
        argv += ["--routing-record", json.dumps(args.routing)]
    return argv


def _spawn_detached(argv: list[str], log_path: Path) -> int:
    """Start ``argv`` as a process that survives this one and its supervisor."""
    log = open(log_path, "ab")  # noqa: SIM115 -- handed to the child, closed below
    try:
        common: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT, "close_fds": True}
        if os.name == "nt":
            flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
            try:
                proc = subprocess.Popen(argv, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **common)
            except OSError:
                # The launcher's job object forbids breakaway: still detach
                # from its console and process group, which is what a
                # console close or Ctrl+C reaches.
                proc = subprocess.Popen(argv, creationflags=flags, **common)
        else:
            proc = subprocess.Popen(argv, start_new_session=True, **common)
        return proc.pid
    finally:
        log.close()


def _cmd_run_detached(args: argparse.Namespace) -> int:
    # Everything the child would refuse is checked HERE, where the caller sees
    # it: a detached child's error only reaches its log file. Found live -- a
    # --cwd outside the default root (the launcher's own cwd) died in the
    # child with nothing but "process gone" to show for it.
    from lazytools.code_bridge import _engines
    from lazytools.connectors.code_support._claude_review import _build_root

    try:
        _read_task(args.task)
        root = _build_root(args.root)
        resolved_cwd = _engines.resolve_cwd(args.cwd, args.root)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Hand the child absolute paths, so nothing depends on its working directory.
    args.root = str(root)
    args.cwd = str(resolved_cwd)
    if args.task.startswith("@"):
        args.task = "@" + str(Path(args.task[1:]).expanduser().resolve())
    job_id = _jobs.new_job_id()
    out_dir = _store.results_dir(_db_path(args))
    log_path = out_dir / f"{job_id}.log"
    pid = _spawn_detached(_child_argv(args, job_id), log_path)
    (out_dir / f"{job_id}.pid").write_text(str(pid), encoding="utf-8")
    if args.json:
        _print_json({"job_id": job_id, "pid": pid, "log": str(log_path), **({"routing": args.routing} if getattr(args, "routing", None) else {})})
    else:
        print(job_id)
        print(f"detached: pid {pid}, log {log_path}")
        db_flag = f' --db "{args.db}"' if args.db else ""
        print(f"wait for it with: lazytools-code-bridge wait {job_id}{db_flag}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        if args.engine is None and args.tier is None:
            raise ValueError("run requires --tier T or --engine E; choose a tier for quota-aware routing or an explicit engine")
        task = _read_task(args.task)
        args.routing = json.loads(args.routing_record) if args.routing_record else None
        if args.tier:
            from lazytools.code_bridge import _engines

            resolved_cwd = _engines.resolve_cwd(args.cwd, args.root)
            selection = _select(args, cwd=str(resolved_cwd))
            if selection.decision.provider is None:
                if args.json:
                    _print_json({**selection.payload(), "error": selection.error()})
                else:
                    _routing.print_selection(selection)
                    print(f"error: {selection.error()}", file=sys.stderr)
                return 2
            args.engine = _routing.bridge_engine(selection.decision.provider)
            args.model, args.effort, args.routing = selection.model, selection.effort, selection.record()
            if not args.json:
                _routing.print_selection(selection)
        elif args.needs or args.review_of:
            raise ValueError("--needs and --review-of require --tier")
        elif args.effort is not None:
            rejection = DEFAULT_POLICY.reject_effort(args.effort, engine=_routing.provider(args.engine), model=args.model)
            if rejection is not None:
                raise ValueError(rejection)
            args.effort = args.effort.strip()
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.detach:
        return _cmd_run_detached(args)
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
            job_id=args.job_id,
            routing=args.routing,
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
                **({"routing": args.routing} if args.routing else {}),
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
    routing = job.get("routing") or {}
    details = f"tier={routing.get('tier') or '-'} model={job.get('model') or 'default'} effort={job.get('effort') or 'default'}"
    return f"{short_id}  {engine:<6}  {status:<16}  {details}  {cwd}  {objective}"


def _select(args: argparse.Namespace, *, cwd: str) -> _routing.Selection:
    return _routing.choose(
        args.tier, cwd=cwd, db_path=_db_path(args), engine=args.engine, session=args.session,
        needs=args.needs, review_of=args.review_of, model=getattr(args, "model", None),
        effort=getattr(args, "effort", None),
        tiers_path=Path(args.tiers).expanduser() if args.tiers else None,
    )


def _cmd_route(args: argparse.Namespace) -> int:
    try:
        cwd = Path(args.cwd).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"--cwd must name an existing directory: {cwd}")
        selection = _select(args, cwd=str(cwd))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        payload = selection.payload()
        if selection.decision.provider is None:
            payload["error"] = selection.error()
        _print_json(payload)
    else:
        _routing.print_selection(selection)
        if selection.decision.provider is None:
            print(f"error: {selection.error()}", file=sys.stderr)
    return 0 if selection.decision.provider is not None else 2


def _cmd_models(args: argparse.Namespace) -> int:
    from lazytools.code_bridge import _models

    try:
        report = _models.inspect_models(
            tiers_path=Path(args.tiers).expanduser() if args.tiers else None,
            probe_claude=args.probe_claude,
        )
    except (OSError, ValueError) as exc:
        if args.json:
            _print_json({"models": [], "probe_claude": args.probe_claude, "mismatches": [], "errors": [str(exc)]})
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(report)
    else:
        _models.print_report(report)
    return 2 if report["mismatches"] or report["errors"] else 0


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


def _detached_pid(db_path: Path | None, job_id: str) -> int | None:
    """The pid `run --detach` recorded for ``job_id`` (full id or unique prefix)."""
    matches = sorted(_store.results_dir(db_path).glob(f"{job_id}*.pid"))
    if len(matches) != 1:
        return None
    try:
        return int(matches[0].read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _cmd_wait(args: argparse.Namespace) -> int:
    """Poll until the job reaches a terminal status, or its detached process is gone.

    Meant to be launched with the caller's own background mechanism (Claude
    Code's run_in_background): if this waiter is killed, the job is not --
    run it again to resume watching."""
    from lazytools.code_bridge._lockfile import _pid_alive

    db_path = _db_path(args)
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    pid = _detached_pid(db_path, args.job_id)
    while True:
        store = _store.build_store(db_path)
        job = _jobs.find_job(store, _store.build_job_registry(store), args.job_id)
        status = job.get("status") if job else None
        if status in _TERMINAL:
            return _cmd_result(args)
        if pid is not None and not _pid_alive(pid):
            # Re-read once: the child may have written its final status just
            # before exiting, between the read above and this liveness check.
            job = _jobs.find_job(store, _store.build_job_registry(store), args.job_id)
            if job and job.get("status") in _TERMINAL:
                return _cmd_result(args)
            message = f"job {args.job_id}: its process {pid} is gone but the job record says {status or 'nothing'}"
            if args.json:
                _print_json({"job_id": args.job_id, "status": "died", "last_status": status, "pid": pid})
            else:
                print(message, file=sys.stderr)
            return 1
        if job is None and pid is None:
            print(f"no job found matching {args.job_id!r}", file=sys.stderr)
            return 1
        if deadline is not None and time.monotonic() >= deadline:
            print(f"job {args.job_id} still {status or 'starting'} after {args.timeout:.0f}s", file=sys.stderr)
            return 3
        time.sleep(args.interval)


# --------------------------------------------------------------------------- #
# pending / approve / reject
# --------------------------------------------------------------------------- #


_JSON_STRING_FIELD = r'"{name}":\s*("(?:[^"\\]|\\.)*")'
#: The opening of a JSON string field whose closing quote never came: the cut
#: went through it. Captures up to the end of that physical line, which is
#: where TieredGate spliced in its "[... elided ...]" marker.
_JSON_STRING_FIELD_HEAD = r'"{name}":\s*"((?:[^"\\\n]|\\.)*)$'
CUT_MARK = " [... cut by the approval gate]"


def _salvage_detail(arguments: str) -> dict[str, str]:
    """Pull ``command``/``reason`` out of an arguments payload TieredGate cut short.

    TieredGate elides long arguments by splicing a marker line into the
    middle of the JSON, which leaves it unparseable and spread over several
    lines; a long escalation (a test run with a long PYTHONPATH, say) then
    showed only "codex-shell" again. ``arguments`` is the WHOLE text after
    "arguments:", every line of it. A field that survived intact is decoded
    on its own; a field the cut went through is shown up to the cut, marked
    as such -- the head of a command is still what a person needs to judge it."""
    import re

    detail: dict[str, str] = {}
    for name in ("command", "reason"):
        match = re.search(_JSON_STRING_FIELD.format(name=name), arguments)
        if match:
            try:
                detail[name] = str(json.loads(match.group(1)))
                continue
            except ValueError:
                pass
        head = re.search(_JSON_STRING_FIELD_HEAD.format(name=name), arguments, flags=re.MULTILINE)
        if head and head.group(1):
            raw = head.group(1).rstrip("\\")
            try:
                text = str(json.loads(f'"{raw}"'))
            except ValueError:
                text = raw
            detail[name] = text + CUT_MARK
    return {k: v for k, v in detail.items() if v}


def _request_detail(prompt: str) -> dict[str, str]:
    """The command and reason a Codex escalation actually carries.

    A Codex sandbox escalation reaches TieredGate as the opaque tool name
    "codex-shell"; the real command line and Codex's own reason sit in the
    JSON on the prompt's "arguments:" line. Without them a person is asked to
    approve "codex-shell" blind. Empty when the prompt has no such payload."""
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("arguments:"):
            continue
        try:
            payload = json.loads(line[len("arguments:") :])
        except ValueError:
            # Hand over the rest of the prompt, not this line alone: an elided
            # payload continues on the lines after the cut marker.
            rest = "\n".join([line[len("arguments:") :], *lines[index + 1 :]])
            return _salvage_detail(rest)
        if not isinstance(payload, dict):
            return {}
        # "command" is what will actually run. "commandActions" is Codex's
        # own lossy parse of it (a pipeline can be summarised by one stage),
        # so it is shown only as a summary, never in place of the command.
        # Found by Codex review.
        actions = payload.get("commandActions") or []
        summary = " ; ".join(str(a["command"]) for a in actions if isinstance(a, dict) and a.get("command"))
        detail = {
            "command": str(payload.get("command") or ""),
            "summary": summary,
            "reason": str(payload.get("reason") or ""),
        }
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


def _add_routing_options(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--tier", choices=["basic", "writing", "thinking", "critical"], required=required)
    parser.add_argument("--tiers", default=None, help="Catalogue TOML (default: ~/.lazytools/model_tiers.toml or packaged ladder).")
    parser.add_argument("--needs", choices=["images"], default=None, help="Require image capability (Codex only).")
    parser.add_argument("--review-of", default=None, metavar="JOB", help="Choose the engine opposite this bridge job's writer.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazytools-code-bridge",
        description="Asynchronous delegation of coding work to Codex / Claude Code, with human approvals over a durable ticket queue.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one coding-engine job to completion in this process.")
    run_p.add_argument("--engine", choices=list(_jobs.ENGINES))
    _add_routing_options(run_p)
    run_p.add_argument("--cwd", required=True, help="Repository (or subdirectory) to work in.")
    run_p.add_argument("--task", required=True, help="Task text, or @path/to/file.")
    run_p.add_argument("--session", default=None, help="Durable session alias to create/resume.")
    run_p.add_argument("--model", default=None)
    run_p.add_argument("--effort", default=None)
    run_p.add_argument(
        "--root", default=None, help="Confinement root for --cwd (default: $LAZYTOOLS_CODE_ROOT or cwd)."
    )
    run_p.add_argument("--json", action="store_true")
    run_p.add_argument(
        "--detach",
        action="store_true",
        help="Start the job in its own process, which outlives this one and the session that launched it, "
        "print its id and return at once. Follow it with `wait`.",
    )
    run_p.add_argument("--job-id", default=None, help=argparse.SUPPRESS)
    run_p.add_argument("--routing-record", default=None, help=argparse.SUPPRESS)
    _add_db_option(run_p)
    run_p.set_defaults(func=_cmd_run)

    route_p = sub.add_parser("route", help="Explain a quota-aware pick; launch nothing.")
    route_p.add_argument("--engine", choices=list(_jobs.ENGINES))
    _add_routing_options(route_p, required=True)
    route_p.add_argument("--cwd", default=".", help="Repository used to resolve session history (default: cwd).")
    route_p.add_argument("--session", default=None)
    route_p.add_argument("--json", action="store_true")
    _add_db_option(route_p)
    route_p.set_defaults(func=_cmd_route)

    models_p = sub.add_parser("models", help="Read live model availability and audit the catalogue and default policy.")
    models_p.add_argument("--tiers", default=None, help="Catalogue path (default: ~/.lazytools/model_tiers.toml or packaged catalogue).")
    models_p.add_argument(
        "--probe-claude", action="store_true",
        help="Opt in to a one-turn Claude probe per catalogue model plus sonnet/opus aliases; consumes a small amount of quota.",
    )
    models_p.add_argument("--json", action="store_true")
    models_p.set_defaults(func=_cmd_models)

    wait_p = sub.add_parser("wait", help="Block until a job ends (or its process dies), then print its outcome.")
    wait_p.add_argument("job_id")
    wait_p.add_argument("--timeout", type=float, default=None, help="Give up after this many seconds (exit 3).")
    wait_p.add_argument("--interval", type=float, default=5.0, help=argparse.SUPPRESS)
    wait_p.add_argument("--json", action="store_true")
    _add_db_option(wait_p)
    wait_p.set_defaults(func=_cmd_wait)

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


def _utf8_output() -> None:
    """Never crash printing a result: a Windows console defaults to a legacy
    codepage, and an engine's answer routinely carries characters it lacks
    (arrows, accents). Found live: `result` died on a "→"."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


__all__ = ["build_parser", "main"]
