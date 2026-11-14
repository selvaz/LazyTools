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
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lazytools.code_bridge import (
    _brief,
    _cloud,
    _cloud_envs,
    _cloud_land,
    _cloud_place,
    _concurrency,
    _engines,
    _jobs,
    _progress,
    _routine,
    _routing,
    _store,
)
from lazytools.code_bridge._concurrency import EngineBusy
from lazytools.code_bridge._lockfile import LockHeld
from lazytools.routing.policy import DEFAULT_POLICY, translate_model


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


def _cloud_print(text: str, *, err: bool = False) -> None:
    """Print one line of cloud-job output through the same redaction as the job record."""
    print(_cloud.redact(text), file=sys.stderr if err else sys.stdout)


def _cloud_json(payload: Any) -> None:
    _print_json(_cloud.redact_obj(payload))


def _configure_run_root(args: argparse.Namespace) -> int | None:
    """Resolve the bridge root once and remember its user-visible provenance."""
    try:
        if args.root is not None:
            root = Path(args.root).expanduser().resolve()
            source = "--root flag"
        else:
            config = _concurrency.load_config()
            if config.root is not None:
                root = config.root
                source = f"config {_concurrency.config_path()}"
            else:
                root = Path.cwd().resolve()
                source = "current working directory"
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.root = str(root)
    args.root_source = source
    return None


def _root_error(args: argparse.Namespace, exc: Exception) -> str:
    """Add actionable root provenance only to confinement failures."""
    if "outside the allowed root" not in str(exc):
        return str(exc)
    return (
        f"{exc} (root from {args.root_source}); pass --root PATH or set root in "
        f"{_concurrency.config_path()} to a directory containing the repository"
    )


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


#: Statuses a job record can end in. A record still "running" or
#: "awaiting_approval" whose process is gone is reported as died by `wait`.
_TERMINAL = ("done", "partial", "failed", "interrupted")

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
    # --where local: the child must never re-resolve placement (a [cloud] default_where would send it to the cloud).
    argv = [sys.executable, "-m", "lazytools.code_bridge", "run", "--where", "local", "--engine", args.engine,
            "--cwd", args.cwd]
    argv += ["--task", args.task, "--job-id", job_id]
    argv += ["--timeout", str(args.timeout)]
    argv += ["--max-continuations", str(args.max_continuations)]
    if args.max_wall is not None:
        argv += ["--max-wall", str(args.max_wall)]
    if args.routine_auto is not None:
        argv += ["--routine-auto" if args.routine_auto else "--no-routine-auto"]
    if args.scratch_root is not None:
        argv += ["--scratch-root", args.scratch_root]
    for flag, value in (("--session", args.session), ("--model", args.model), ("--effort", args.effort)):
        if value:
            argv += [flag, value]
    if args.root:
        argv += ["--root", args.root]
    if args.db:
        argv += ["--db", args.db]
    if getattr(args, "routing", None) is not None:
        argv += ["--routing-record", json.dumps(args.routing)]
    if getattr(args, "wait_for_slot", False):
        argv += ["--wait-for-slot"]
    # The launcher already checked the brief and printed its findings.
    argv += ["--brief-checked"]
    return argv


def _spawn_detached(argv: list[str], log_path: Path) -> int:
    """Start ``argv`` as a process that survives this one and its supervisor."""
    log = open(log_path, "ab")  # noqa: SIM115 -- handed to the child, closed below
    try:
        common: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
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


def _cmd_run_detached(args: argparse.Namespace, task: str) -> int:
    # Everything the child would refuse is checked HERE, where the caller sees
    # it: a detached child's error only reaches its log file. Found live -- a
    # --cwd outside the default root (the launcher's own cwd) died in the
    # child with nothing but "process gone" to show for it.
    from lazytools.code_bridge import _engines
    from lazytools.connectors.code_support._claude_review import _build_root

    try:
        root = _build_root(args.root)
        resolved_cwd = _engines.resolve_cwd(args.cwd, args.root)
    except (OSError, ValueError) as exc:
        print(f"error: {_root_error(args, exc)}", file=sys.stderr)
        return 2
    # A busy engine is refused HERE too, naming the running job, rather than
    # in a child whose refusal only reaches its log. The child re-checks
    # (and takes the slot) itself; this is the early, visible answer.
    if not getattr(args, "wait_for_slot", False):
        try:
            slot_config = _concurrency.load_config()
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        holders = _concurrency.engine_at_limit(args.engine, slot_config)
        if holders:
            limit = slot_config.limits.get(args.engine) or len(holders)
            print(str(EngineBusy(args.engine, limit, holders)), file=sys.stderr)
            return 2
    # Hand the child absolute paths, so nothing depends on its working directory.
    args.root = str(root)
    args.cwd = str(resolved_cwd)
    job_id = _jobs.new_job_id()
    out_dir = _store.results_dir(_db_path(args))
    if args.task.startswith("@"):
        # The brief was read (and checked) once, by the caller. The child gets that
        # exact text as a snapshot, not the file: an edit, a swap or a delete
        # between this check and the child's start must not change what runs.
        snapshot = out_dir / f"{job_id}.brief.md"
        try:
            snapshot.write_bytes(task.encode("utf-8"))
        except OSError as exc:
            print(f"error: could not snapshot the brief: {exc}", file=sys.stderr)
            return 2
        args.task = "@" + str(snapshot.resolve())  # absolute: nothing depends on the child's cwd
    log_path = out_dir / f"{job_id}.log"
    pid = _spawn_detached(_child_argv(args, job_id), log_path)
    (out_dir / f"{job_id}.pid").write_text(str(pid), encoding="utf-8")
    if args.json:
        _print_json(
            {
                "job_id": job_id,
                "pid": pid,
                "log": str(log_path),
                **({"routing": args.routing} if getattr(args, "routing", None) else {}),
            }
        )
    else:
        print(job_id)
        print(f"detached: pid {pid}, log {log_path}")
        db_flag = f' --db "{args.db}"' if args.db else ""
        print(f"wait for it with: lazytools-code-bridge wait {job_id}{db_flag}")
    return 0


def _check_brief_before_launch(args: argparse.Namespace, task: str) -> int | None:
    """Print the brief's findings; return an exit code only when ``--strict`` refuses the launch.

    Findings go to stderr, one line each: stdout's first line is the job id
    (and ``--json`` callers parse the rest), so the check must not add to it.
    """
    if args.no_brief_check:
        print("brief-check: skipped (--no-brief-check)", file=sys.stderr)
        return None
    try:
        cwd = _engines.resolve_cwd(args.cwd, args.root)
    except (OSError, ValueError) as exc:
        print(f"error: {_root_error(args, exc)}", file=sys.stderr)
        return 2
    findings = _brief.check_brief(task, engine=args.engine, cwd=cwd, root=args.root)
    for finding in findings:
        print(f"brief-check: {finding.format()}", file=sys.stderr)
    errors = [f for f in findings if f.severity == "error"]
    if args.strict and errors:
        print(
            f"error: brief refused by --strict: {len(errors)} error(s) "
            f"({', '.join(f.code for f in errors)}); fix the brief, or drop --strict to launch anyway",
            file=sys.stderr,
        )
        return 2
    return None


_CLOUD_INCOMPATIBLE = (
    ("needs", "--needs"),
    ("session", "--session"),
    ("effort", "--effort"),
    ("max_wall", "--max-wall"),
    ("routine_auto", "--routine-auto"),
    ("scratch_root", "--scratch-root"),
    ("detach", "--detach"),
    ("wait_for_slot", "--wait-for-slot"),
)


def _cmd_run_cloud(args: argparse.Namespace) -> int:
    """Submit the brief to a cloud agent; return as soon as the task handle is recorded."""
    import uuid

    review_meta: dict[str, Any] | None = None
    job_id = args.job_id or uuid.uuid4().hex
    try:
        if not args.allow_cloud:
            raise ValueError(
                "--where cloud requires --allow-cloud: a cloud agent's commands run on the provider's machine and "
                "cannot pass through the local approval gate one by one (see docs/code-bridge.md, 'Approvals')"
            )
        if args.engine is None and not args.review_of:
            raise ValueError("--where cloud requires an explicit --engine (quota routing is local-only; "
                             "--review-of picks the opposite engine by itself)")
        if args.routine and args.engine and args.engine != "claude":
            raise ValueError("--routine is only for --where cloud --engine claude")
        used = [flag for name, flag in _CLOUD_INCOMPATIBLE if getattr(args, name, None) not in (None, False)]
        if used:
            raise ValueError(f"--where cloud does not take {', '.join(used)}")
        if args.review_of and (args.land or args.pr is not None or args.open_pr):
            raise ValueError("--review-of posts a comment on the reviewed job's PR; it does not take --land, --pr or --open-pr")
        task = _read_task(args.task) if args.task else ""
        if not task and not args.review_of:
            raise ValueError("run requires --task (or --review-of, for a cloud review)")
        cwd = _engines.resolve_cwd(args.cwd, args.root)
        branch = args.branch
        extra_meta: dict[str, Any] = {}
        if args.review_of:
            store = _store.build_store(_db_path(args))
            prep = _cloud_land.prepare_review(
                store, _store.build_job_registry(store), args.review_of, job_id=job_id,
                repo=_cloud.repo_slug(_cloud.repo_root(cwd)), engine=args.engine, extra=task,
                root=_cloud.repo_root(cwd),
            )
            if branch and branch != prep["branch"]:
                raise ValueError(f"--branch {branch!r} conflicts with the reviewed branch {prep['branch']!r}")
            args.engine, branch, task = prep["engine"], prep["branch"], prep["task"]
            review_meta = extra_meta["review"] = prep["review"]
        if args.routine and args.engine != "claude":
            raise ValueError("--routine is only for --where cloud --engine claude")
    except (OSError, ValueError, _cloud.CloudError) as exc:
        _cloud_print(f"error: {_root_error(args, exc)}", err=True)
        return 2
    if not args.brief_checked and not review_meta:
        refusal = _check_brief_before_launch(args, task)
        if refusal is not None:
            return refusal
    if _cloud.redact(task) != task:
        print("warning: the brief contains credential-looking text; it is masked in the job record, but the cloud "
              "agent receives the brief as written -- rotate the secret if it is real", file=sys.stderr)
    try:
        job = _cloud.submit_job(
            engine=args.engine, cwd=cwd, task=task, branch=branch, db_path=_db_path(args), job_id=job_id,
            approvals=tuple(args.approve_cloud or ()), routine=args.routine, pr=args.pr, open_pr=args.open_pr,
            land=args.land, rebase_ok=args.rebase_ok, model=args.model, tier=args.tier, title=args.title,
            extra_meta=extra_meta,
        )
    except (_cloud.CloudError, ValueError) as exc:
        _cloud_print(f"error: {exc}", err=True)
        return 2
    placement = getattr(args, "placement", None)
    if args.json:
        payload = {k: job.get(k) for k in ("job_id", "status", "where", "engine", "repo", "env_id", "branch",
                                           "cloud_handle", "cloud_url", "model", "pr_number", "pr_url", "land", "open_pr")}
        if review_meta:
            payload["review_of"] = review_meta["of"]
        if placement is not None and placement.requested == _cloud_place.WHERE_AUTO:
            payload["placement"] = placement.payload()
        _cloud_json(payload)
    else:
        _cloud_print(str(job["job_id"]))
        _cloud_print(f"cloud: {job['engine']} task {job.get('cloud_url') or job.get('cloud_handle')} "
              f"(repo {job['repo']}, branch {job['branch']}, env {job['env_id']})")
        db_flag = f' --db "{args.db}"' if args.db else ""
        if job.get("model"):
            _cloud_print(f"model: {job['model']}")
        elif job.get("model_note"):
            _cloud_print(f"model: {job['model_note']}")
        if job.get("pr_number"):
            _cloud_print(f"PR target: #{job['pr_number']} ({job.get('pr_url')}), working on its head branch {job['branch']}")
        elif job.get("open_pr"):
            _cloud_print(f"PR target: a new branch and PR against {job['branch']} when landed")
        if review_meta:
            _cloud_print(f"review of job {review_meta['of'][:8]} ({review_meta['author']}'s work, branch "
                         f"{review_meta['branch']}); the verdict is posted to "
                         f"{review_meta.get('pr_url') or 'its PR (none recorded: the review stays in the result)'}")
        if job.get("engine") == "claude":
            if job.get("environment_note"):
                _cloud_print(f"note: {job['environment_note']}")
            _cloud_print("note: Claude cloud sessions have no status API; the bridge watches origin for the "
                         "claude/ branch the session pushes")
        if job.get("land") or review_meta:
            _cloud_print("landing/posting is automatic: `lazytools-code-bridge wait` or `lazytools-code-bridge cloud-sync` "
                         "does it when the task finishes")
        _cloud_print(f"wait for it with: lazytools-code-bridge wait {job['job_id']}{db_flag}")
    return 0


def _resolve_placement(args: argparse.Namespace) -> int | None:
    """Turn ``--where`` (or ``[cloud] default_where``) into ``args.where`` = local|cloud; ``auto`` explains itself."""
    try:
        config = _cloud.load_config()
        requested = args.where or config.default_where
        placement = _cloud_place.decide(requested, windows=config.busy_windows, engine=args.engine)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if placement.requested == _cloud_place.WHERE_AUTO:
        print(placement.line(), file=sys.stderr)  # stderr: stdout's first line is the job id
    args.where = placement.where
    args.placement = placement
    return None


def _cmd_run(args: argparse.Namespace) -> int:
    refusal = _configure_run_root(args)
    if refusal is not None:
        return refusal
    refusal = _resolve_placement(args)
    if refusal is not None:
        return refusal
    if args.where == _cloud.WHERE_CLOUD:
        return _cmd_run_cloud(args)
    for name, flag in (("branch", "--branch"), ("allow_cloud", "--allow-cloud"), ("approve_cloud", "--approve-cloud"),
                       ("routine", "--routine"), ("pr", "--pr"), ("open_pr", "--open-pr"), ("land", "--land"),
                       ("title", "--title"),
                       ("rebase_ok", "--rebase-ok")):
        if getattr(args, name, None) not in (None, False, []):
            print(f"error: {flag} is only for --where cloud", file=sys.stderr)
            return 2
    if args.task is None:
        print("error: run requires --task", file=sys.stderr)
        return 2
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("--timeout must be a finite number greater than 0")
        if not 0 <= args.max_continuations <= _jobs.MAX_CONTINUATIONS:
            raise ValueError(f"--max-continuations must be an integer from 0 to {_jobs.MAX_CONTINUATIONS}")
        if args.max_wall is not None and (not math.isfinite(args.max_wall) or args.max_wall <= 0):
            raise ValueError("--max-wall must be a finite number greater than 0")
        _routine.load_policy(enabled=args.routine_auto, scratch_root=args.scratch_root)
        if args.engine is None and args.tier is None:
            raise ValueError(
                "run requires --tier T or --engine E; choose a tier for quota-aware routing or an explicit engine"
            )
        task = _read_task(args.task)
        args.routing = json.loads(args.routing_record) if args.routing_record else None
        if not args.tier and args.session:
            from lazytools.code_bridge import _engines

            resolved_cwd = _engines.resolve_cwd(args.cwd, args.root)
            _routing.check_session_engine(
                cwd=str(resolved_cwd), session=args.session, engine=args.engine, db_path=_db_path(args)
            )
        if not args.tier and args.model is not None and args.engine:
            args.model = translate_model(args.model, _routing.provider(args.engine))
        if args.tier:
            from lazytools.code_bridge import _engines

            resolved_cwd = _engines.resolve_cwd(args.cwd, args.root)
            selection = _select(args, cwd=str(resolved_cwd))
            if selection.decision.provider is None and args.wait_for_slot and _slot_excluded(selection):
                # Nothing free is eligible, but a busy engine would be: with
                # --wait-for-slot, pick as if its slot were free and queue.
                selection = _select(args, cwd=str(resolved_cwd), respect_slots=False)
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
            rejection = DEFAULT_POLICY.reject_effort(
                args.effort, engine=_routing.provider(args.engine), model=args.model
            )
            if rejection is not None:
                raise ValueError(rejection)
            args.effort = args.effort.strip()
    except (OSError, ValueError) as exc:
        print(f"error: {_root_error(args, exc)}", file=sys.stderr)
        return 2
    if not args.brief_checked:
        refusal = _check_brief_before_launch(args, task)
        if refusal is not None:
            return refusal
    if args.detach:
        return _cmd_run_detached(args, task)
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
            timeout=args.timeout,
            max_continuations=args.max_continuations,
            max_wall=args.max_wall,
            routine_auto=args.routine_auto,
            scratch_root=args.scratch_root,
            wait_for_slot=args.wait_for_slot,
            on_slot_wait=_announce_slot_wait,
        )
    except (LockHeld, EngineBusy) as exc:
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
    if _cloud.is_cloud(job):
        details = f"where=cloud task={job.get('cloud_url') or job.get('cloud_handle') or '-'} branch={job.get('branch') or '-'}"
        return _cloud.redact(f"{short_id}  {engine:<6}  {status:<16}  {details}  {cwd}  {objective}")
    return f"{short_id}  {engine:<6}  {status:<16}  {details}  {cwd}  {objective}"


def _announce_slot_wait(busy: EngineBusy) -> None:
    jobs = ", ".join(str(h.get("job_id", "?")) for h in busy.holders)
    print(f"waiting for a {busy.engine} slot (limit {busy.limit}; running: {jobs})", flush=True)


def _slot_excluded(selection: _routing.Selection) -> bool:
    return any("engine_at_concurrency_limit" in why for why in selection.decision.ineligible.values())


def _select(args: argparse.Namespace, *, cwd: str, respect_slots: bool = True) -> _routing.Selection:
    return _routing.choose(
        args.tier,
        cwd=cwd,
        db_path=_db_path(args),
        engine=args.engine,
        session=args.session,
        needs=args.needs,
        review_of=args.review_of,
        model=getattr(args, "model", None),
        effort=getattr(args, "effort", None),
        tiers_path=Path(args.tiers).expanduser() if args.tiers else None,
        respect_slots=respect_slots,
    )


def _cmd_route(args: argparse.Namespace) -> int:
    placement = None
    try:
        cwd = Path(args.cwd).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"--cwd must name an existing directory: {cwd}")
        if args.where:
            config = _cloud.load_config()
            placement = _cloud_place.decide(args.where, windows=config.busy_windows, engine=args.engine)
        # A cloud placement does not use local slots, so the pick ignores them.
        selection = _select(args, cwd=str(cwd), respect_slots=not (placement and placement.where == "cloud"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        payload = selection.payload()
        if selection.decision.provider is None:
            payload["error"] = selection.error()
        if placement is not None:
            payload["placement"] = placement.payload()
        _print_json(payload)
    else:
        if placement is not None:
            print(placement.line())
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
    if _cloud.is_cloud(job):
        job = _cloud.refresh(store, registry, job)
    if args.json:
        (_cloud_json if _cloud.is_cloud(job) else _print_json)(job)
    else:
        print(_job_line(job))
        if _cloud.is_cloud(job):
            if job.get("cloud_status"):
                _cloud_print(f"cloud status: {job['cloud_status']}")
            if job.get("last_poll_error"):
                _cloud_print(f"last poll failed: {job['last_poll_error']}", err=True)
        if "routine_auto" in job:
            print(f"routine-auto: {'enabled' if job['routine_auto'] else 'disabled'}; {job.get('routine_rule_count', 0)} active rules")
        if job.get("turns"):
            print(_progress.history_text(job["turns"]))
    return 0


def _cmd_result(args: argparse.Namespace) -> int:
    store = _store.build_store(_db_path(args))
    registry = _store.build_job_registry(store)
    job = _jobs.find_job(store, registry, args.job_id)
    if job is None:
        print(f"no job found matching {args.job_id!r}", file=sys.stderr)
        return 1
    if _cloud.is_cloud(job):
        job = _cloud.refresh(store, registry, job)
    status = job.get("status", "?")
    # Same exit code either way -- a caller scripting on exit status must
    # not see a failed job reported as success merely because it asked for
    # --json. Found by Codex review (it previously always returned 0 here).
    exit_code = 1 if status in ("partial", "failed", "interrupted") else 0
    if args.json:
        (_cloud_json if _cloud.is_cloud(job) else _print_json)(job)
        return exit_code
    if _cloud.is_cloud(job):
        _cloud_print(_cloud.result_text(job))
        return exit_code
    print(f"[{status}] job {job.get('job_id')}")
    if job.get("turns"):
        print(_progress.history_text(job["turns"]))
    if job.get("result"):
        print(job["result"])
    elif job.get("error"):
        print(f"error: {job['error']}", file=sys.stderr)
    else:
        print("(no result yet)")
    return exit_code


def _detached_job(db_path: Path | None, job_id: str) -> tuple[str, int] | None:
    """Resolve a detached pid file to its full job id and pid."""
    matches = sorted(_store.results_dir(db_path).glob(f"{job_id}*.pid"))
    if len(matches) != 1:
        return None
    try:
        return matches[0].stem, int(matches[0].read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _detached_pid(db_path: Path | None, job_id: str) -> int | None:
    """The pid `run --detach` recorded for ``job_id`` (full id or unique prefix)."""
    detached = _detached_job(db_path, job_id)
    return detached[1] if detached else None


def _process_matches_bridge(pid: int) -> bool | None:
    """Check identity when available; psutil is optional, never a dependency."""
    try:
        import psutil

        process = psutil.Process(pid)
        argv = process.cmdline()
    except ImportError:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            return None
    except (psutil.Error, OSError):
        return None
    if not argv:
        return None
    executable = Path(argv[0]).name.lower()
    module = any(argv[i : i + 2] == ["-m", "lazytools.code_bridge"] for i in range(1, len(argv) - 1))
    entry_point = any(Path(arg).name in ("lazytools-code-bridge", "lazytools-code-bridge.exe") for arg in argv[1:2])
    return executable.startswith("python") and (module or entry_point)


def _kill_process_tree(pid: int, *, owns_group: bool = False) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=30, check=True
        )
        return
    try:
        group = os.getpgid(pid)
        # Group-kill only a group the job verifiably owns (it led its own session);
        # a pipeline leader's group also holds its peers (e.g. `tee`).
        if owns_group and group == pid:
            os.killpg(group, signal.SIGKILL)
            return
    except ProcessLookupError:
        return
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _isolate_process_group() -> None:
    if sys.platform == "win32":
        return
    # A detached child already leads its own session. A pipeline leader has
    # pgid == pid but shares the group, and setsid fails for it: then the job
    # does not own a group and `cancel` kills only its pid.
    if os.getsid(0) == os.getpid():
        return
    try:
        os.setsid()
    except OSError:
        pass


def _cmd_cancel(args: argparse.Namespace) -> int:
    from lazytools.code_bridge._lockfile import _pid_alive

    db_path = _db_path(args)
    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    job = _jobs.find_job(store, registry, args.job_id)
    startup = job is None
    pid: int | None
    if job is None:
        detached = _detached_job(db_path, args.job_id)
        if detached is None:
            print(f"no job found matching {args.job_id!r}", file=sys.stderr)
            return 1
        job_id, pid = detached
        meta = _store.read_meta(store, job_id) or {}
        job = _jobs.find_job(store, registry, job_id) or {
            "job_id": job_id,
            "kind": meta.get("engine", "unknown"),
            "objective": meta.get("objective", ""),
        }
    else:
        job_id = str(job["job_id"])
        pid = _detached_pid(db_path, job_id) or job.get("pid")
    if _cloud.is_cloud(job):
        return _cancel_cloud(store, registry, job, args.reason)
    if job.get("status") in _TERMINAL:
        print(f"job {job['job_id']} already {job['status']}")
        return 0
    if not isinstance(pid, int) or pid <= 0:
        print(f"job {job_id}: no process id recorded; cannot cancel", file=sys.stderr)
        return 1
    if _pid_alive(pid):
        if _process_matches_bridge(pid) is False:
            print(f"job {job_id}: pid {pid} is not a Python code-bridge process; refusing to kill", file=sys.stderr)
            return 1
        # The job may have completed during the identity check.
        latest = _jobs.find_job(store, registry, job_id)
        if latest and latest.get("status") in _TERMINAL:
            print(f"job {job_id} already {latest['status']}")
            return 0
        try:
            _kill_process_tree(pid, owns_group=bool(job.get("owns_process_group")))
        except (OSError, subprocess.SubprocessError) as exc:
            if _pid_alive(pid):
                print(f"job {job_id}: could not kill pid {pid}: {exc}", file=sys.stderr)
                return 1
        deadline = time.monotonic() + 5
        while _pid_alive(pid):
            if time.monotonic() >= deadline:
                print(f"job {job_id}: pid {pid} is still alive after cancellation", file=sys.stderr)
                return 1
            time.sleep(0.05)
    # A final write may precede process exit or race the kill. Preserve it,
    # including when the pid was already dead at the first liveness check.
    latest = _jobs.find_job(store, registry, job_id)
    if latest and latest.get("status") in _TERMINAL:
        print(f"job {job_id} already {latest['status']}")
        return 0
    if latest is not None:
        job = latest
    if startup:
        meta = _store.read_meta(store, job_id) or {}
        meta.setdefault("job_id", job_id)
        meta.setdefault("pid", pid)
        _store.write_meta(store, job_id, meta)
    message = "cancelled by operator during startup" if startup else "cancelled by operator"
    error = _jobs.interrupt_job(store, registry, job, f"{message}: {args.reason}", db_path=db_path)
    print(f"[interrupted] job {job_id}\n{error}")
    return 0


def _cmd_cloud_stop(args: argparse.Namespace) -> int:
    """Stop provider work, rather than only dropping local tracking."""
    from lazytools.code_bridge import _cloud_stop

    store = _store.build_store(_db_path(args))
    registry = _store.build_job_registry(store)
    targets: list[tuple[str, str, dict[str, Any] | None]] = []
    errors: list[dict[str, str]] = []
    if args.all_running:
        try:
            targets.extend(("codex", handle, None) for handle in _cloud_stop.codex_running())
        except Exception as exc:
            errors.append({"provider": "codex", "status": "failed", "reason": _cloud.redact(str(exc))})
        for job in _cloud.active_jobs(store):
            handle = job.get("cloud_handle")
            if handle:
                try:
                    provider, ident = _cloud_stop.resolve_handle(str(handle))
                    targets.append((provider, ident, job))
                except Exception as exc:
                    errors.append({"handle": _cloud.redact(str(handle)), "status": "failed", "reason": _cloud.redact(str(exc))})
    else:
        found = _jobs.find_job(store, registry, args.target)
        value = str(found.get("cloud_handle")) if found and found.get("cloud_handle") else args.target
        try:
            provider, ident = _cloud_stop.resolve_handle(value)
            targets.append((provider, ident, found))
        except Exception as exc:
            errors.append({"handle": _cloud.redact(value), "status": "failed", "reason": _cloud.redact(str(exc))})

    unique: dict[tuple[str, str], tuple[str, str, dict[str, Any] | None]] = {}
    for target in targets:
        key = target[:2]
        previous = unique.get(key)
        unique[key] = target if target[2] is not None or previous is None else previous
    results: list[dict[str, Any]] = []
    for provider, handle, owner_job in unique.values():
        if args.dry_run:
            result: dict[str, Any] = {"provider": provider, "handle": handle, "status": "would stop"}
        else:
            try:
                result = _cloud_stop.stop(provider, handle)
                if owner_job is not None and result.get("status") in ("stopped", "already finished"):
                    _cloud.mark_interrupted(store, registry, str(owner_job["job_id"]), "cloud stop requested by operator")
                    result["job_id"] = owner_job["job_id"]
            except Exception as exc:
                result = {"provider": provider, "handle": handle, "status": "failed", "reason": _cloud.redact(str(exc))}
        results.append(result)
    results.extend(errors)
    safe = _cloud.redact_obj(results)
    if args.json:
        _print_json(safe)
    else:
        for item in safe:
            suffix = f": {item['reason']}" if item.get("reason") else ""
            print(f"{item.get('provider', 'cloud')} {item.get('handle', '')}: {item['status']}{suffix}")
    return 1 if any(item.get("status") == "failed" for item in safe) else 0


def _cancel_cloud(store: Any, registry: Any, job: dict[str, Any], reason: str) -> int:
    """Stop tracking a cloud job locally (frees its cloud place). The task itself keeps running remotely."""
    job_id = str(job["job_id"])
    if job.get("status") in _TERMINAL:
        _cloud_print(f"job {job_id} already {job['status']}")
        return 0
    if not _cloud.mark_interrupted(store, registry, job_id, f"cancelled by operator: {reason}"):
        latest = _jobs.find_job(store, registry, job_id) or job
        _cloud_print(f"job {job_id} already {latest.get('status')}")
        return 0
    where = job.get("cloud_url") or job.get("cloud_handle") or "the provider's web UI"
    _cloud_print(f"[interrupted] job {job_id}\nno longer tracked locally; the cloud task is NOT stopped -- cancel it at {where}")
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    """Poll until the job reaches a terminal status, or its detached process is gone.

    Meant to be launched with the caller's own background mechanism (Claude
    Code's run_in_background): if this waiter is killed, the job is not --
    run it again to resume watching."""
    from lazytools.code_bridge._lockfile import _pid_alive

    db_path = _db_path(args)
    first = _store.build_store(db_path)
    first_job = _jobs.find_job(first, _store.build_job_registry(first), args.job_id)
    if first_job is not None and _cloud.is_cloud(first_job):
        timeout = _cloud.DEFAULT_CLOUD_WAIT_TIMEOUT if args.timeout is None else args.timeout
        job = _cloud.wait_job(lambda: _store.build_store(db_path), str(first_job["job_id"]),
                              timeout=timeout, interval=args.interval)
        if job is not None and job.get("status") in _TERMINAL:
            owed_failed = False
            if _cloud_land.needs_finishing(job):
                # --land / --review-of: finishing the job (landing it, posting its review) is part of waiting for it.
                finish_store = _store.build_store(db_path)
                row = _cloud_land.process_job(finish_store, _store.build_job_registry(finish_store), job,
                                              db_path=db_path)
                _cloud_print(f"{row['action']}: {row['detail']}", err=True)
                owed_failed = row["action"].endswith("-failed")
            code = _cmd_result(args)
            return code or (1 if owed_failed else 0)
        if job is None:
            print(f"job {args.job_id}: its record disappeared while waiting", file=sys.stderr)
            return 1
        errors = int(job.get("poll_errors_in_a_row") or 0)
        if errors:
            _cloud_print(f"job {args.job_id} still unknown after {errors} failed status poll(s); last error: "
                         f"{job.get('last_poll_error')}", err=True)
            return 4
        print(f"job {args.job_id} still {job.get('status')} after {timeout:.0f}s", file=sys.stderr)
        return 3

    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    pid = _detached_pid(db_path, args.job_id)
    while True:
        store = _store.build_store(db_path)
        job = _jobs.find_job(store, _store.build_job_registry(store), args.job_id)
        status = job.get("status") if job else None
        if status in _TERMINAL:
            return _cmd_result(args)
        owner = pid if pid is not None else (job.get("pid") if job else None)
        if type(owner) is int and not _pid_alive(owner):
            # Re-read once: the child may have written its final status just
            # before exiting, between the read above and this liveness check.
            job = _jobs.find_job(store, _store.build_job_registry(store), args.job_id)
            if job and job.get("status") in _TERMINAL:
                return _cmd_result(args)
            message = f"job {args.job_id}: its process {owner} is gone but the job record says {status or 'nothing'}"
            if args.json:
                _print_json({"job_id": args.job_id, "status": "died", "last_status": status, "pid": owner})
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


def _cmd_resync(args: argparse.Namespace) -> int:
    store = _store.build_store(_db_path(args))
    registry = _store.build_job_registry(store)
    job = _jobs.find_job(store, registry, args.job_id)
    if job is None:
        print(f"no job found matching {args.job_id!r}", file=sys.stderr)
        return 1
    if not _cloud.is_cloud(job):
        print(f"job {args.job_id} is a local job", file=sys.stderr)
        return 2
    try:
        job = _cloud.resync(store, registry, job)
    except _cloud.CloudError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        _cloud_json(job)
    else:
        print(_job_line(job))
        if job.get("cloud_status"):
            _cloud_print(f"cloud status: {job['cloud_status']}")
    return 0 if job.get("status") == "done" else (1 if job.get("status") in _TERMINAL else 4)


def _cloud_base_branch(outcome: dict[str, Any]) -> str:
    return str(outcome.get("base_branch") or "<branch>")


def _cmd_setup_skill(args: argparse.Namespace) -> int:
    """Print (or install) the bundled agent skill that walks a user through cloud setup."""
    from lazytools.skills.agent_skills import agent_skill_path, install_agent_skill

    name = "code-bridge-cloud-setup"
    if args.install is None:
        print(agent_skill_path(name))
        return 0
    try:
        target = install_agent_skill(name, Path(args.install) if args.install else None, overwrite=args.force)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"installed {target}")
    print('ask your assistant: "set up lazytools-code-bridge cloud jobs" (Claude Code picks the skill up by name)')
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    """Apply a finished cloud task in a fresh worktree/branch; never pushes."""
    db_path = _db_path(args)
    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    job = _jobs.find_job(store, registry, args.job_id)
    if job is None:
        print(f"no job found matching {args.job_id!r}", file=sys.stderr)
        return 1
    if not _cloud.is_cloud(job):
        print(f"job {job['job_id']} is a local job: its changes are already in {job.get('cwd')}", file=sys.stderr)
        return 2
    if args.land:
        return _cmd_land(args, store, registry, job, db_path)
    try:
        job = _cloud.refresh(store, registry, job)
        outcome = _cloud.apply_job(
            store,
            registry,
            job,
            db_path=db_path,
            branch=args.branch,
            worktree=Path(args.worktree) if args.worktree else None,
            rebase=args.rebase,
            source=args.source,
        )
    except _cloud.CloudError as exc:
        _cloud_print(f"error: {exc}", err=True)
        return 1
    if args.json:
        _cloud_json(outcome)
        return 0
    origin = f"{outcome['source_branch']} @ {outcome['base']}" if outcome.get("source_branch") else outcome["base"]
    _cloud_print(f"applied cloud job {outcome['job_id']} in {outcome['worktree']} (branch {outcome['branch']}, from {origin})")
    for line in outcome["changed"]:
        _cloud_print(f"  {line}")
    if outcome["output"]:
        _cloud_print(outcome["output"])
    if outcome.get("rebased_from"):
        _cloud_print(f"note: --rebase: applied onto the current head of origin/{_cloud_base_branch(outcome)} "
              f"({outcome['base'][:12]}), not the task's base {outcome['rebased_from'][:12]}; review the result carefully")
    _cloud_print(f"not pushed. Review, commit, then push only after approval: git -C \"{outcome['worktree']}\" push -u origin {outcome['branch']}"
                 f" -- or land it in one step: lazytools-code-bridge apply {outcome['job_id']} --land")
    return 0


def _cmd_land(args: argparse.Namespace, store: Any, registry: Any, job: dict[str, Any], db_path: Path | None) -> int:
    """``apply --land``: commit, push, then update the recorded PR or open a new one."""
    try:
        job = _cloud.refresh(store, registry, job)
        outcome = _cloud_land.land_job(
            store, registry, job, db_path=db_path, rebase_ok=True if args.rebase_ok else None, branch=args.branch,
            worktree=Path(args.worktree) if args.worktree else None, source=args.source,
        )
    except _cloud.CloudError as exc:
        _cloud_print(f"error: not landed: {exc}", err=True)
        return 1
    if args.json:
        _cloud_json(outcome)
        return 0
    if outcome.get("already"):
        _cloud_print(f"job {outcome['job_id']} was already landed")
    if outcome.get("note"):
        _cloud_print(outcome["note"])
    if outcome.get("pushed_sha"):
        _cloud_print(f"landed cloud job {outcome['job_id']}: pushed {outcome['branch']} @ {str(outcome['pushed_sha'])[:12]}")
    if outcome.get("pr_url"):
        _cloud_print(f"PR: {outcome['pr_url']}")
    if outcome.get("force_added_ignored"):
        _cloud_print("force-added ignored cloud diff paths: " + ", ".join(outcome["force_added_ignored"]))
    return 0


def _cmd_cloud_sync(args: argparse.Namespace) -> int:
    """Reconcile every live cloud job: poll, land the finished ones that asked for it, record failures."""
    try:
        rows = _cloud_land.sync(_db_path(args))
    except (_cloud.CloudError, ValueError) as exc:
        _cloud_print(f"error: {exc}", err=True)
        return 1
    failed = any(row["action"].endswith("-failed") for row in rows)
    if args.json:
        _cloud_json({"jobs": rows, "ok": not failed})
    elif not rows:
        print("no live cloud jobs to reconcile")
    else:
        for row in rows:
            _cloud_print(f"{str(row['job_id'])[:8]}  {row['engine']:<6}  {row['status']:<12}  {row['action']:<14}  {row['detail']}")
    return 1 if failed else 0


def _cmd_cloud_envs(args: argparse.Namespace) -> int:
    try:
        config = _cloud.load_config()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        slug: str | None = _cloud.repo_slug(_cloud.repo_root(Path(args.cwd).expanduser().resolve()))
    except (_cloud.CloudError, OSError):
        slug = None
    report = _cloud_envs.list_envs(config, current_repo=slug, probe_codex=not args.no_probe)
    if args.json:
        _cloud_json(report)
    else:
        _cloud_print(_cloud_envs.format_envs(report))
    return 0


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


def _cmd_prune_sessions(args: argparse.Namespace) -> int:
    from lazytools.code_bridge import _prune
    from lazytools.connectors.code_support._common import session_registry

    if not math.isfinite(args.older_than_days) or args.older_than_days < 0:
        print("--older-than-days must be a finite number >= 0", file=sys.stderr)
        return 2
    roots = _prune.default_roots()
    if args.claude_root:
        roots["claude"] = Path(args.claude_root)
    if args.codex_root:
        roots["codex"] = Path(args.codex_root)
    engines = ("claude", "codex") if args.engine == "all" else (args.engine,)
    # A session a named alias still points at stays: deleting it would break --session resume.
    protected = {row["native_id"] for row in session_registry().entries() if row.get("native_id")}
    found: list[_prune.Transcript] = []
    for engine in engines:
        scan = _prune.claude_transcripts if engine == "claude" else _prune.codex_transcripts
        found.extend(scan(roots[engine]))
    chosen, kept = _prune.candidates(found, older_than_days=args.older_than_days, protected_ids=protected)
    size = sum(item.total_size for item in chosen)
    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "older_than_days": args.older_than_days,
        "candidates": len(chosen),
        "candidate_mb": round(size / 1e6, 1),
        "by_engine": {e: sum(1 for i in chosen if i.engine == e) for e in engines},
        "kept": kept,
    }
    if args.apply:
        count, freed, errors = _prune.delete(chosen)
        summary.update(deleted=count, freed_mb=round(freed / 1e6, 1), errors=errors)
    if args.json:
        _print_json(summary)
        return 1 if summary.get("errors") else 0
    print(
        f"{summary['mode']}: {len(chosen)} automated session(s) older than {args.older_than_days:g} days, "
        f"{summary['candidate_mb']} MB ({', '.join(f'{e} {n}' for e, n in summary['by_engine'].items())})"
    )
    print("kept: " + ", ".join(f"{n} {why}" for why, n in kept.items()))
    if args.apply:
        print(f"deleted {summary['deleted']}, freed {summary['freed_mb']} MB")
        for error in summary["errors"]:
            print(f"error: {error}", file=sys.stderr)
        return 1 if summary["errors"] else 0
    print("nothing deleted; re-run with --apply to delete")
    return 0


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #


def _add_routing_options(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--tier", choices=["basic", "writing", "thinking", "critical"], required=required)
    parser.add_argument(
        "--tiers", default=None, help="Catalogue TOML (default: ~/.lazytools/model_tiers.toml or packaged ladder)."
    )
    parser.add_argument("--needs", choices=["images"], default=None, help="Require image capability (Codex only).")
    parser.add_argument(
        "--review-of", default=None, metavar="JOB", help="Choose the engine opposite this bridge job's writer."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazytools-code-bridge",
        description="Asynchronous delegation of coding work to Codex / Claude Code, with human approvals over a durable ticket queue.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one coding-engine job to completion in this process.")
    run_p.add_argument("--engine", choices=list(_jobs.ENGINES))
    run_p.add_argument(
        "--where",
        choices=list(_cloud_place.WHERE_CHOICES),
        default=None,
        help="local: run the engine on this machine. cloud: submit the brief to the engine's cloud "
        "agent (Codex Cloud, or a Claude Code cloud session) and return at once; uses no local engine slot and "
        "cannot read local data. auto: cloud inside a [cloud] busy_windows range or when no local engine slot is "
        "free, else local. Default: [cloud] default_where, else local. Setup: docs/code-bridge-cloud-setup.md.",
    )
    run_p.add_argument("--pr", type=int, default=None, metavar="N",
                       help="--where cloud only: work on pull request N's head branch; the job records the PR.")
    run_p.add_argument("--open-pr", action="store_true",
                       help="--where cloud only: when landed, push a new branch and open a PR against --branch.")
    run_p.add_argument("--title", default=None,
                       help="--where cloud only: explicit PR, landing commit, and squash subject (default: # Goal text).")
    run_p.add_argument(
        "--land",
        action="store_true",
        help="--where cloud only: land the result automatically when the job finishes (`wait` or `cloud-sync`): "
        "commit, push, update the --pr PR or open a new one. Implies --open-pr without --pr.",
    )
    run_p.add_argument(
        "--rebase-ok",
        action="store_true",
        help="--where cloud only: let landing proceed when the base branch moved since submit (refused otherwise). "
        "Never force-pushes.",
    )
    run_p.add_argument(
        "--branch", default=None, help="--where cloud only: remote branch the cloud agent starts from (default: current)."
    )
    run_p.add_argument(
        "--routine",
        default=None,
        help="--where cloud --engine claude only: fire this [cloud.claude.routines] entry (its API trigger token is "
        "read from the env var the config names) instead of running `claude --cloud`.",
    )
    run_p.add_argument(
        "--allow-cloud",
        action="store_true",
        help="--where cloud only, required: acknowledge that the cloud agent's commands run outside the local "
        "approval gate (only its diff comes back, through `apply`).",
    )
    run_p.add_argument(
        "--approve-cloud",
        action="append",
        choices=sorted(_cloud.GATED_ACTIONS),
        default=None,
        help="--where cloud only: approve up front an action the brief asks for that the local approval gate "
        "would hold (classified by the same rule table); repeatable. Without it such a brief is refused.",
    )
    _add_routing_options(run_p)
    run_p.add_argument("--cwd", required=True, help="Repository (or subdirectory) to work in.")
    run_p.add_argument("--task", default=None, help="Task text, or @path/to/file (required, except for a cloud --review-of).")
    run_p.add_argument("--session", default=None, help="Durable session alias to create/resume.")
    run_p.add_argument("--model", default=None)
    run_p.add_argument("--effort", default=None)
    run_p.add_argument(
        "--timeout",
        type=float,
        default=_engines.DEFAULT_TIMEOUT,
        help=f"Per-turn timeout in seconds, greater than 0 (default: {_engines.DEFAULT_TIMEOUT:g}).",
    )
    run_p.add_argument("--max-continuations", type=int, default=_jobs.DEFAULT_MAX_CONTINUATIONS,
                       help="Automatic continuations after a timeout with committed/pushed progress (0-10, default: 3).")
    run_p.add_argument("--max-wall", type=float, default=None,
                       help="Total job wall-clock cap in seconds, including approvals (default: unlimited).")
    run_p.add_argument("--routine-auto", action=argparse.BooleanOptionalAction, default=None,
                       help="Enable the audited routine-auto approval tier (default: on, or [routine_auto] enabled).")
    run_p.add_argument("--scratch-root", default=None,
                       help="Routine pytest/verifier output root (default: [routine_auto] scratch_root, or D:/Temp on Windows).")
    run_p.add_argument(
        "--root", default=None, help="Confinement root for --cwd (default: root in code-bridge.toml, then cwd)."
    )
    run_p.add_argument("--json", action="store_true")
    run_p.add_argument(
        "--detach",
        action="store_true",
        help="Start the job in its own process, which outlives this one and the session that launched it, "
        "print its id and return at once. Follow it with `wait`.",
    )
    run_p.add_argument(
        "--wait-for-slot",
        action="store_true",
        help="If the engine is at its machine-wide concurrency limit (default: codex 1, claude unlimited; "
        "[concurrency] in ~/.lazytools/code-bridge.toml), queue until a slot frees instead of refusing.",
    )
    run_p.add_argument(
        "--strict",
        action="store_true",
        help="Refuse to launch (exit 2, no job record) when the brief check finds an error. "
        "Without it, findings are printed and the job still launches.",
    )
    run_p.add_argument(
        "--no-brief-check", action="store_true", help="Skip the pre-launch brief check (a note is printed)."
    )
    run_p.add_argument("--brief-checked", action="store_true", help=argparse.SUPPRESS)
    run_p.add_argument("--job-id", default=None, help=argparse.SUPPRESS)
    run_p.add_argument("--routing-record", default=None, help=argparse.SUPPRESS)
    _add_db_option(run_p)
    run_p.set_defaults(func=_cmd_run)

    route_p = sub.add_parser("route", help="Explain a quota-aware pick; launch nothing.")
    route_p.add_argument("--engine", choices=list(_jobs.ENGINES))
    route_p.add_argument("--where", choices=list(_cloud_place.WHERE_CHOICES), default=None,
                         help="Also show where the job would run: auto explains its local-vs-cloud decision.")
    _add_routing_options(route_p, required=True)
    route_p.add_argument("--cwd", default=".", help="Repository used to resolve session history (default: cwd).")
    route_p.add_argument("--session", default=None)
    # Same overrides as `run`, so the exact pick (bucket, provider, effort check) can be dry-run.
    route_p.add_argument("--model", default=None)
    route_p.add_argument("--effort", default=None)
    route_p.add_argument("--json", action="store_true")
    _add_db_option(route_p)
    route_p.set_defaults(func=_cmd_route)

    models_p = sub.add_parser("models", help="Read live model availability and audit the catalogue and default policy.")
    models_p.add_argument(
        "--tiers", default=None, help="Catalogue path (default: ~/.lazytools/model_tiers.toml or packaged catalogue)."
    )
    models_p.add_argument(
        "--probe-claude",
        action="store_true",
        help="Opt in to a one-turn Claude probe per catalogue model plus sonnet/opus aliases; consumes a small amount of quota.",
    )
    models_p.add_argument("--json", action="store_true")
    models_p.set_defaults(func=_cmd_models)

    wait_p = sub.add_parser("wait", help="Block until a job ends (or its process dies), then print its outcome.")
    wait_p.add_argument("job_id")
    wait_p.add_argument("--timeout", type=float, default=None,
                        help="Give up after this many seconds (exit 3). Cloud jobs default to 7200.")
    wait_p.add_argument("--interval", type=float, default=5.0, help=argparse.SUPPRESS)
    wait_p.add_argument("--json", action="store_true")
    _add_db_option(wait_p)
    wait_p.set_defaults(func=_cmd_wait)

    resync_p = sub.add_parser("resync", help="Re-check a cloud job that an older bridge failed after a poll error.")
    resync_p.add_argument("job_id")
    resync_p.add_argument("--json", action="store_true")
    _add_db_option(resync_p)
    resync_p.set_defaults(func=_cmd_resync)

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

    cancel_p = sub.add_parser(
        "cancel", help="Stop a job's process tree, record workspace evidence, and reject its open approvals."
    )
    cancel_p.add_argument("job_id", help="Full job id or unique prefix.")
    cancel_p.add_argument("--reason", default="requested cancellation", help="Reason recorded with the interruption.")
    _add_db_option(cancel_p)
    cancel_p.set_defaults(func=_cmd_cancel)

    stop_p = sub.add_parser(
        "cloud-stop", help="Stop a Codex Cloud task or request that a Claude Code cloud session stop."
    )
    stop_target = stop_p.add_mutually_exclusive_group(required=True)
    stop_target.add_argument("target", nargs="?", help="Task URL, task/session id, or bridge job id.")
    stop_target.add_argument("--all-running", action="store_true", help="Stop account-wide Codex tasks and running bridge cloud jobs.")
    stop_p.add_argument("--dry-run", action="store_true", help="List targets without making provider requests.")
    stop_p.add_argument("--json", action="store_true", help="Emit machine-readable results for Agent Radar.")
    _add_db_option(stop_p)
    stop_p.set_defaults(func=_cmd_cloud_stop)

    result_p = sub.add_parser(
        "result", help="Show one job's result or error, including workspace evidence for unfinished jobs."
    )
    result_p.add_argument("job_id")
    result_p.add_argument("--json", action="store_true")
    _add_db_option(result_p)
    result_p.set_defaults(func=_cmd_result)

    apply_p = sub.add_parser(
        "apply",
        help="Apply a finished cloud job's diff in a NEW git worktree on a NEW branch (never your checkout); does not push.",
    )
    apply_p.add_argument("job_id")
    apply_p.add_argument("--branch", default=None, help="New branch name (default: cloud/<job id prefix>).")
    apply_p.add_argument("--worktree", default=None, help="New worktree path (default: beside the Store, cloud-worktrees/<job>).")
    apply_p.add_argument(
        "--rebase",
        action="store_true",
        help="Apply onto the branch's current head even if it moved since the task was submitted (refused otherwise).",
    )
    apply_p.add_argument(
        "--source",
        default=None,
        help="Claude cloud jobs only: the claude/ branch to check out when several new ones were found.",
    )
    apply_p.add_argument(
        "--land",
        action="store_true",
        help="After applying: commit (message from the job's brief), push, then update the job's recorded PR or open "
        "a new one (gh). A Claude cloud job is verified and recorded instead. Refuses a moved base unless "
        "--rebase-ok; never force-pushes.",
    )
    apply_p.add_argument("--rebase-ok", action="store_true", help="With --land: allow a moved base branch.")
    apply_p.add_argument("--json", action="store_true")
    _add_db_option(apply_p)
    apply_p.set_defaults(func=_cmd_apply)

    sync_p = sub.add_parser(
        "cloud-sync",
        help="Reconcile every live cloud job: poll the provider, land finished jobs submitted with --land, post "
        "finished reviews, record failures with the provider's reason. Safe to run from a scheduler.",
    )
    sync_p.add_argument("--json", action="store_true")
    _add_db_option(sync_p)
    sync_p.set_defaults(func=_cmd_cloud_sync)

    envs_p = sub.add_parser(
        "cloud-envs", help="List Codex Cloud environments (and their linked repo) and the Claude cloud environment(s)."
    )
    envs_p.add_argument("--cwd", default=".", help="Repository to check for a mapped Codex environment (default: cwd).")
    envs_p.add_argument("--no-probe", action="store_true", help="Do not call `codex cloud list`; show the config only.")
    envs_p.add_argument("--json", action="store_true")
    envs_p.set_defaults(func=_cmd_cloud_envs)

    skill_p = sub.add_parser(
        "setup-skill",
        help="Print the path of the agent skill (SKILL.md) that guides an LLM through cloud setup, or install it.",
    )
    skill_p.add_argument(
        "--install",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="Copy it to DIR/code-bridge-cloud-setup/SKILL.md (default DIR: ~/.claude/skills).",
    )
    skill_p.add_argument("--force", action="store_true", help="Replace an existing installed copy.")
    skill_p.set_defaults(func=_cmd_setup_skill)

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

    prune_p = sub.add_parser(
        "prune-sessions",
        help="Delete old transcripts of automated (SDK / LazyBridge) Claude Code and Codex sessions. Dry run by default.",
    )
    prune_p.add_argument("--older-than-days", type=float, default=7.0)
    prune_p.add_argument("--engine", choices=["all", "claude", "codex"], default="all")
    prune_p.add_argument("--apply", action="store_true", help="Actually delete (default: list only).")
    prune_p.add_argument("--claude-root", default=None, help="Default: ~/.claude/projects.")
    prune_p.add_argument("--codex-root", default=None, help="Default: ~/.codex/sessions.")
    prune_p.add_argument("--json", action="store_true")
    prune_p.set_defaults(func=_cmd_prune_sessions)

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
    if argv is None and os.name != "nt" and args.command == "run" and not args.detach:
        # Keep engine descendants in our own session/group for `cancel`.
        _isolate_process_group()
    return args.func(args)


__all__ = ["build_parser", "main"]
