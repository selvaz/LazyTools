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
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from lazybridge import Store
from lazybridge.ext.approval import ApprovalTicket

from lazytools.code_bridge import (
    _autonomy,
    _brief,
    _capabilities,
    _cloud,
    _cloud_envs,
    _cloud_land,
    _cloud_place,
    _cloud_preflight,
    _coder,
    _concurrency,
    _credentials,
    _dispatch,
    _engines,
    _events,
    _freshness,
    _jobs,
    _launch,
    _progress,
    _registry,
    _routing,
    _store,
    _task,
    _watch,
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


def _add_registry_option(parser: argparse.ArgumentParser) -> None:
    _add_db_option(parser)
    parser.add_argument(
        "--registry-db",
        type=Path,
        help="Approval registry path (default: sibling <db stem>.registry.sqlite for a non-default --db, "
        "otherwise the shared projects registry).",
    )


def _selected_registry_db(args: argparse.Namespace) -> Path | None:
    return _registry.isolated_path(_db_path(args), args.registry_db)


def _ticket_job_store(args: argparse.Namespace) -> Any:
    db_path = _db_path(args)
    path = db_path or _store.default_db_path()
    if path.is_file():
        return _store.build_store(db_path)
    # A caller can point both stores at the same file. Looking up a ticket
    # must not create the registry indirectly through the job store.
    return Store()


def _operator_registries(
    args: argparse.Namespace,
    store: Any,
    *,
    jobs: Iterable[dict[str, Any]] | None = None,
) -> Iterator[tuple[_registry.Registry, set[str] | None]]:
    primary = _registry.Registry(store, _selected_registry_db(args))
    yield primary, None
    if args.registry_db is not None:
        return
    if jobs is None:
        jobs = _jobs.list_jobs(store, all_jobs=True)
    # Older jobs have no saved path and still use the shared projects registry.
    # Scope discovered registries so a custom job store cannot see other jobs' tickets.
    jobs_by_path: dict[Path | None, set[str]] = {}
    for job in jobs:
        if job_id := job.get("job_id"):
            jobs_by_path.setdefault(_registry.recorded_path(job), set()).add(str(job_id))
    for path, job_ids in jobs_by_path.items():
        registry = _registry.Registry(store, path)
        if registry.path != primary.path:
            yield registry, job_ids


def _pending_tickets(
    args: argparse.Namespace,
    store: Any,
    *,
    jobs: Iterable[dict[str, Any]] | None = None,
) -> list[ApprovalTicket]:
    tickets = {
        ticket.approval_id: ticket
        for registry, job_ids in _operator_registries(args, store, jobs=jobs)
        for ticket in registry.pending()
        if job_ids is None or _registry.job_ref(ticket) in job_ids
    }
    return sorted(tickets.values(), key=lambda ticket: ticket.created_at)


def _print_json(payload: Any) -> None:
    # One compact line, not pretty-printed: easier for a caller to parse
    # line-by-line (the `run` command's output in particular is job-id
    # line + one JSON line, and a multi-line payload would break that).
    print(json.dumps(payload, default=str), flush=True)


def _safe_print(text: str) -> None:
    """Print text even when a legacy Windows console cannot encode it."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="backslashreplace").decode(encoding), flush=True)


def _job_identity(job_id: str, *, where: str, engine: Any, cwd: Any, **extra: Any) -> dict[str, Any]:
    return {"job_id": job_id, "short_id": job_id[:8], "where": where, "engine": engine, "cwd": str(cwd), **extra}


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
            source = "flag (--root)"
        elif env_root := os.environ.get("LAZYTOOLS_CODE_ROOT"):
            root = Path(env_root).expanduser().resolve()
            source = "env (LAZYTOOLS_CODE_ROOT)"
        else:
            workspace = _concurrency.load_workspace()
            if workspace.root is not None:
                root = workspace.root
                source = f"config ({_concurrency.config_path()})"
            else:
                root = Path.cwd().resolve()
                source = "cwd"
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
        f"LAZYTOOLS_CODE_ROOT or {_concurrency.config_path()} to a directory containing the repository"
    )


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


#: Statuses a job record can end in. A record still "running" or
#: "awaiting_approval" whose process is gone is reported as died by `wait`.
_TERMINAL = ("done", "partial", "failed", "interrupted")


def _record_intervention(store: Any, job_id: str, kind: str, reason: str | None = None) -> None:
    """Best-effort audit write; an audit failure must not undo an operator action."""
    try:
        _store.record_intervention(store, job_id, kind, reason)
    except Exception as exc:
        print(f"warning: action succeeded but its {kind} intervention could not be recorded: {exc}", file=sys.stderr)


# Windows process-creation flags for a child that must outlive its launcher:
# no console tie to the parent, its own process group (so a Ctrl+C or a
# console close aimed at the launcher does not reach it), and out of the
# launcher's job object when that object allows it -- a job object is what
# lets a supervising process (e.g. a Claude Code background shell being
# reaped) take its whole tree down with it.
# CREATE_NO_WINDOW gives the child a NEW, hidden console instead of none
# (DETACHED_PROCESS). Console programs the job starts (git, npm, pytest,
# powershell, the engines) inherit that hidden console; with no console at
# all, Windows allocates a visible one for each of them, which on a machine
# whose default terminal is Windows Terminal opens one tab per command.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _child_argv(args: argparse.Namespace, job_id: str) -> list[str]:
    # --where local: the child must never re-resolve placement (a [cloud] default_where would send it to the cloud).
    argv = [
        sys.executable,
        "-m",
        "lazytools.code_bridge",
        "run",
        "--where",
        "local",
        "--engine",
        args.engine,
        "--cwd",
        args.cwd,
    ]
    argv += ["--task", args.task, "--job-id", job_id]
    argv += ["--timeout", str(args.timeout)]
    argv += ["--max-continuations", str(args.max_continuations)]
    if args.max_wall is not None:
        argv += ["--max-wall", str(args.max_wall)]
    for flag, value in (("--session", args.session), ("--model", args.model), ("--effort", args.effort)):
        if value:
            argv += [flag, value]
    if args.root:
        argv += ["--root", args.root]
    if args.db:
        argv += ["--db", args.db]
    if args.registry_db is not None:
        argv += ["--registry-db", str(args.registry_db)]
    if getattr(args, "routing", None) is not None:
        argv += ["--routing-record", json.dumps(args.routing)]
    if getattr(args, "wait_for_slot", False):
        argv += ["--wait-for-slot"]
    if args.profile != "default":
        argv += ["--profile", args.profile]
    for flag, value in (("--network", args.network), ("--launch-mode", args.launch_mode)):
        if value is not None:
            argv += [flag, value]
    if args.review_of or args.intentional_review:
        argv += ["--intentional-review"]
    if args.review_of:
        argv += ["--review-of", args.review_of]
    if args.task_id:
        argv += ["--task-id", args.task_id, "--claim-token", args.claim_token]
    argv += ["--detached-binding"]
    # The launcher already checked the brief and printed its findings.
    argv += ["--brief-checked"]
    return argv


def _spawn_detached(argv: list[str], log_path: Path) -> int:
    """Start ``argv`` as a process that survives this one and its supervisor."""
    from lazytools.code_bridge._credentials import job_environment

    log = open(log_path, "ab")  # noqa: SIM115 -- handed to the child, closed below
    try:
        common: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
            "env": job_environment(os.environ),
        }
        if os.name == "nt":
            flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
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


def _cmd_run_detached(args: argparse.Namespace, task: str, registry_db: Path | None) -> int:
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
    try:
        claim = _task.load(args.task_id, args.claim_token, state="intent")
        resolver = claim.launch if claim else _launch.resolve_launch
        launch = resolver(
            args.engine,
            resolved_cwd,
            args.profile,
            args.network,
            args.launch_mode,
            review=bool(args.review_of or args.intentional_review),
        )
    except (OSError, ValueError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    store = _store.build_store(_db_path(args))
    args.registry_db = registry_db
    if args.db:
        args.db = str(Path(args.db).expanduser().resolve())
    store.write(
        _launch.DETACH_PREFIX + job_id,
        _jobs.detach_binding(
            launch,
            args.engine,
            resolved_cwd,
            task,
            args.session,
            claim,
            registry_db=registry_db,
        ),
    )
    pid = _spawn_detached(_child_argv(args, job_id), log_path)
    (out_dir / f"{job_id}.pid").write_text(str(pid), encoding="utf-8")
    if args.json:
        _print_json(
            {
                **_job_identity(job_id, where="local", engine=args.engine, cwd=args.cwd),
                "pid": pid,
                "log": str(log_path),
                **launch.metadata(),
                **(claim.binding() if claim else {}),
                **({"registry_db": str(registry_db)} if registry_db is not None else {}),
                **({"routing": args.routing} if getattr(args, "routing", None) else {}),
            }
        )
    else:
        print(job_id)
        print(f"detached: pid {pid}, log {log_path} ({launch.mode}, {launch.policy.profile})")
        if registry_db is not None:
            print(f"approval registry: {registry_db}")
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


def _check_freshness_before_launch(args: argparse.Namespace) -> int | None:
    """Warn on stderr when the branch is behind its upstream; refuse only with ``--require-fresh``."""
    if args.no_freshness_check:
        return None
    try:
        cwd = _engines.resolve_cwd(args.cwd, args.root)
    except (OSError, ValueError):
        return None  # the launch reports a bad --cwd itself
    result = _freshness.check_freshness(cwd)
    if not result.stale():
        return None
    print(f"freshness: warn {result.message()}", file=sys.stderr)
    if args.require_fresh:
        print("error: launch refused by --require-fresh; update the branch, or drop --require-fresh", file=sys.stderr)
        return 2
    return None


_CLOUD_INCOMPATIBLE = (
    ("needs", "--needs"),
    ("effort", "--effort"),
    ("max_wall", "--max-wall"),
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
            raise ValueError(
                "--where cloud requires an explicit --engine (quota routing is local-only; "
                "--review-of picks the opposite engine by itself)"
            )
        if args.routine and args.engine and args.engine != "claude":
            raise ValueError("--routine is only for --where cloud --engine claude")
        if args.profile != "default":
            raise ValueError("--profile is only available for local jobs")
        used = [flag for name, flag in _CLOUD_INCOMPATIBLE if getattr(args, name, None) not in (None, False)]
        if used:
            raise ValueError(f"--where cloud does not take {', '.join(used)}")
        if args.session or args.detach:
            print("warning: --session and --detach are ignored for cloud jobs", file=sys.stderr)
        if args.review_of and (args.land or args.pr is not None or args.open_pr):
            raise ValueError(
                "--review-of posts a comment on the reviewed job's PR; it does not take --land, --pr or --open-pr"
            )
        task = _read_task(args.task) if args.task else ""
        if not task and not args.review_of:
            raise ValueError("run requires --task (or --review-of, for a cloud review)")
        cwd = _engines.resolve_cwd(args.cwd, args.root)
        branch = args.branch
        extra_meta: dict[str, Any] = {"task_id": args.task_id} if args.task_id else {}
        if args.review_of:
            store = _store.build_store(_db_path(args))
            rows = _jobs.list_jobs(store, all_jobs=True) if args.session else []
            prep = _cloud_land.prepare_review(
                store,
                _store.build_job_registry(store),
                args.review_of,
                job_id=job_id,
                repo=_cloud.repo_slug(_cloud.repo_root(cwd)),
                engine=args.engine,
                extra=task,
                root=_cloud.repo_root(cwd),
                session_history=bool(args.session)
                and _routing.session_hint(rows, str(cwd), args.session).engine is not None,
            )
            if branch and branch != prep["branch"]:
                raise ValueError(f"--branch {branch!r} conflicts with the reviewed branch {prep['branch']!r}")
            args.engine, branch, task = prep["engine"], prep["branch"], prep["task"]
            review_meta = extra_meta["review"] = prep["review"]
            extra_meta["review_independence"] = review_meta["independence"]
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
        print(
            "warning: the brief contains credential-looking text; it is masked in the job record, but the cloud "
            "agent receives the brief as written -- rotate the secret if it is real",
            file=sys.stderr,
        )
    try:
        job = _cloud.submit_job(
            engine=args.engine,
            cwd=cwd,
            task=task,
            branch=branch,
            db_path=_db_path(args),
            job_id=job_id,
            routine=args.routine,
            pr=args.pr,
            open_pr=args.open_pr,
            land=args.land,
            rebase_ok=args.rebase_ok,
            model=args.model,
            tier=args.tier,
            title=args.title,
            network=args.network == "on",
            extra_meta=extra_meta,
        )
    except _cloud.CloudEnvUnmapped as exc:
        if args.json:
            _cloud_json(exc.payload())
        else:
            _cloud_print(f"error: {exc}", err=True)
        return 2
    except (_cloud.CloudError, ValueError) as exc:
        _cloud_print(f"error: {exc}", err=True)
        return 2
    placement = getattr(args, "placement", None)
    if args.json:
        payload = {
            k: job.get(k)
            for k in (
                "job_id",
                "status",
                "where",
                "engine",
                "repo",
                "env_id",
                "branch",
                "cloud_handle",
                "cloud_url",
                "model",
                "pr_number",
                "pr_url",
                "land",
                "open_pr",
            )
        }
        payload.update(short_id=str(job["job_id"])[:8], cwd=str(cwd), task_url=job.get("cloud_url"))
        if review_meta:
            payload["review_of"] = review_meta["of"]
            payload["independence"] = review_meta["independence"]
        if placement is not None and placement.requested == _cloud_place.WHERE_AUTO:
            payload["placement"] = placement.payload()
        _cloud_json(payload)
    else:
        _cloud_print(str(job["job_id"]))
        _cloud_print(
            f"cloud: {job['engine']} task {job.get('cloud_url') or job.get('cloud_handle')} "
            f"(repo {job['repo']}, branch {job['branch']}, env {job['env_id']})"
        )
        db_flag = f' --db "{args.db}"' if args.db else ""
        if job.get("model"):
            _cloud_print(f"model: {job['model']}")
        elif job.get("model_note"):
            _cloud_print(f"model: {job['model_note']}")
        if job.get("pr_number"):
            _cloud_print(
                f"PR target: #{job['pr_number']} ({job.get('pr_url')}), working on its head branch {job['branch']}"
            )
        elif job.get("open_pr"):
            _cloud_print(f"PR target: a new branch and PR against {job['branch']} when landed")
        if review_meta:
            _cloud_print(
                f"review of job {review_meta['of'][:8]} ({review_meta['author']}'s work, branch "
                f"{review_meta['branch']}); the verdict is posted to "
                f"{review_meta.get('pr_url') or 'its PR (none recorded: the review stays in the result)'}"
            )
        if job.get("engine") == "claude":
            if job.get("environment_note"):
                _cloud_print(f"note: {job['environment_note']}")
            _cloud_print(
                "note: Claude cloud sessions have no status API; the bridge watches origin for the "
                "claude/ branch the session pushes"
            )
        if job.get("land") or review_meta:
            _cloud_print(
                "landing/posting is automatic: `lazytools-code-bridge wait` or `lazytools-code-bridge cloud-sync` "
                "does it when the task finishes"
            )
        _cloud_print(f"wait for it with: lazytools-code-bridge wait {job['job_id']}{db_flag}")
    return 0


def _resolve_placement(args: argparse.Namespace) -> int | None:
    """Turn ``--where`` (or ``[cloud] default_where``) into ``args.where`` = local|cloud; ``auto`` explains itself."""
    try:
        config = _cloud.load_config()
        requested = args.where or config.default_where
        cwd = (
            _engines.resolve_cwd(args.cwd, args.root)
            if requested in ("auto", "cloud") and args.engine == "codex"
            else None
        )
        placement = _cloud_place.decide_for_repo(requested, config=config, engine=args.engine, cwd=cwd)
    except _cloud.CloudEnvUnmapped as exc:
        if args.json:
            _print_json(exc.payload())
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, _cloud.CloudError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if placement.requested == _cloud_place.WHERE_AUTO or placement.requested != placement.where:
        print(placement.line(), file=sys.stderr)  # stderr: stdout's first line is the job id
    args.where = placement.where
    args.placement = placement
    if placement.where == _cloud.WHERE_LOCAL and placement.requested != _cloud.WHERE_LOCAL:
        # The caller acknowledged that auto placement may use cloud.  If it
        # instead selected local, that acknowledgement is harmless rather than
        # a local-only option error.
        args.allow_cloud = False
    return None


def _cmd_run(args: argparse.Namespace) -> int:
    refusal = _configure_run_root(args)
    if refusal is not None:
        return refusal
    refusal = _resolve_placement(args)
    if refusal is not None:
        return refusal
    if args.where == _cloud.WHERE_CLOUD:
        # A cloud job may carry --task-id as a label (dispatch records it); the claim
        # fence (--claim-token) is checked by local launches only.
        if (
            args.launch_mode != "auto"
            or args.profile not in _launch.LEGACY_PROFILES
            or args.claim_token
        ):
            print("error: core launch mode, profiles, and task claims require local placement", file=sys.stderr)
            return 2
        return _cmd_run_cloud(args)
    for name, flag in (
        ("branch", "--branch"),
        ("allow_cloud", "--allow-cloud"),
        ("routine", "--routine"),
        ("pr", "--pr"),
        ("open_pr", "--open-pr"),
        ("land", "--land"),
        ("title", "--title"),
        ("rebase_ok", "--rebase-ok"),
    ):
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
        if args.engine is None and args.tier is None:
            raise ValueError(
                "run requires --tier T or --engine E; choose a tier for quota-aware routing or an explicit engine"
            )
        task = _read_task(args.task)
        registry_db = _selected_registry_db(args)
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
            if selection.decision.provider is None and args.wait_for_slot and selection.slot_limited:
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
        elif args.needs:
            raise ValueError("--needs requires --tier")
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
        refusal = _check_freshness_before_launch(args)
        if refusal is not None:
            return refusal
    if args.detach:
        return _cmd_run_detached(args, task, registry_db)
    job_id_holder: dict[str, str] = {}

    def _announce(job_id: str) -> None:
        job_id_holder["job_id"] = job_id
        if args.json:
            _print_json(
                {
                    **_job_identity(job_id, where="local", engine=args.engine, cwd=args.cwd),
                    **({"registry_db": str(registry_db)} if registry_db is not None else {}),
                    **({"routing": args.routing} if args.routing else {}),
                }
            )
        else:
            print(job_id, flush=True)  # first line: the caller can capture this immediately
            if registry_db is not None:
                print(f"approval registry: {registry_db}", flush=True)

    def _slot_wait(busy: EngineBusy) -> None:
        _announce_slot_wait(busy, err=args.json)

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
            registry_db=registry_db,
            on_job_id=_announce,
            job_id=args.job_id,
            routing=args.routing,
            timeout=args.timeout,
            max_continuations=args.max_continuations,
            max_wall=args.max_wall,
            wait_for_slot=args.wait_for_slot,
            on_slot_wait=_slot_wait,
            profile=args.profile,
            network=args.network,
            launch_mode=args.launch_mode,
            intentional_review=bool(args.review_of or args.intentional_review),
            review_of=args.review_of,
            detached_binding=args.detached_binding,
            task_id=args.task_id,
            claim_token=args.claim_token,
        )
    except (LockHeld, EngineBusy) as exc:
        if args.detached_binding:
            _jobs.record_detached_refusal(_db_path(args), args.job_id, task, str(exc))
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, ImportError) as exc:
        if args.detached_binding:
            _jobs.record_detached_refusal(_db_path(args), args.job_id, task, str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        _print_json(
            {
                "event": "result",
                "job_id": result.job_id,
                "status": result.status,
                "result_path": str(result.result_path) if result.result_path else None,
                "error": result.error,
                **({"independence": args.routing.get("independence")} if args.routing else {}),
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


def _announce_slot_wait(busy: EngineBusy, *, err: bool = False) -> None:
    jobs = ", ".join(str(h.get("job_id", "?")) for h in busy.holders)
    print(
        f"waiting for a {busy.engine} slot (limit {busy.limit}; running: {jobs})",
        file=sys.stderr if err else sys.stdout,
        flush=True,
    )


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
        if args.where:
            refusal = _configure_run_root(args)
            if refusal is not None:
                return refusal
        if args.where in ("auto", "cloud") and args.engine == "codex":
            # Resolve against the configured root, exactly as ``run`` does.
            cwd = Path(_engines.resolve_cwd(args.cwd, args.root)).resolve()
        else:
            cwd = Path(args.cwd).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"--cwd must name an existing directory: {cwd}")
        if args.where:
            config = _cloud.load_config()
            placement = _cloud_place.decide_for_repo(args.where, config=config, engine=args.engine, cwd=cwd)
        # A cloud placement does not use local slots, so the pick ignores them.
        selection = _select(args, cwd=str(cwd), respect_slots=not (placement and placement.where == "cloud"))
    except _cloud.CloudEnvUnmapped as exc:
        if args.json:
            _print_json({**exc.payload(), "placement": "would fail: unmapped"})
        else:
            print(f"would fail: unmapped: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, _cloud.CloudError) as exc:
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


def _cmd_capabilities(args: argparse.Namespace) -> int:
    try:
        entries = _capabilities.load_catalog()
    except OSError as exc:
        print(f"error: could not read the capabilities catalog: {exc}", file=sys.stderr)
        return 1

    query = args.query.casefold()
    for entry in entries:
        if query in entry["capability"].casefold() or query in entry["call"].casefold():
            print(entry["section"], entry["capability"], entry["call"], entry["status"], sep="\t")
    return 0


def _cmd_jobs(args: argparse.Namespace) -> int:
    store = _store.build_store(_db_path(args))
    rows = _jobs.list_jobs(store, all_jobs=args.all)
    if args.json:
        for row in rows:
            _print_json(row)
        return 0
    if not rows:
        print("no jobs yet" if args.all else "no active jobs (pass --all for history)")
        return 0
    for row in rows:
        _safe_print(_job_line(row))
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
    else:
        _attach_card_audit(args, store, job)
    if args.json:
        (_cloud_json if _cloud.is_cloud(job) else _print_json)(job)
    else:
        print(_job_line(job))
        if job.get("registry_db"):
            print(f"approval registry: {job['registry_db']}")
        if _cloud.is_cloud(job):
            if job.get("cloud_status"):
                _cloud_print(f"cloud status: {job['cloud_status']}")
            if job.get("last_poll_error"):
                _cloud_print(f"last poll failed: {job['last_poll_error']}", err=True)
        _print_card_audit(job)
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
    else:
        _attach_card_audit(args, store, job)
    status = job.get("status", "?")
    # Same exit code either way -- a caller scripting on exit status must
    # not see a failed job reported as success merely because it asked for
    # --json. Found by Codex review (it previously always returned 0 here).
    exit_code = 1 if status in ("partial", "failed", "interrupted") else 0
    diff = None
    if _cloud.is_cloud(job) and job.get("status") == "done" and job.get("cloud_handle"):
        try:
            job, diff = _cloud.capture_report(store, registry, job)
        except _cloud.CloudError:
            pass
    if args.json:
        (_cloud_json if _cloud.is_cloud(job) else _print_json)(job)
        return exit_code
    if _cloud.is_cloud(job):
        _cloud_print(_cloud.result_text(job, diff=diff))
        return exit_code
    print(f"[{status}] job {job.get('job_id')}")
    _print_card_audit(job)
    if job.get("turns"):
        print(_progress.history_text(job["turns"]))
    if job.get("result"):
        print(job["result"])
    elif job.get("error"):
        print(f"error: {job['error']}", file=sys.stderr)
    else:
        print("(no result yet)")
    return exit_code


def _attach_card_audit(args: argparse.Namespace, store: Any, job: dict[str, Any]) -> None:
    path = _registry.recorded_path(job) or _selected_registry_db(args)
    if path is not None:
        job["registry_db"] = str(path)
    job["card_approvals"] = _registry.Registry(store, path).card_audit(job) if job.get("coder_card_ids") else []


def _print_card_audit(job: dict[str, Any]) -> None:
    for receipt in job.get("card_approvals", []):
        label = receipt.get("command") or receipt.get("tool") or receipt["action"]
        _safe_print(f"coder card {receipt['card_id']} approved {label}")


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
        result = _cancel_cloud(store, registry, job, args.reason)
        if result == 0:
            _record_intervention(store, job_id, "cancel", args.reason)
        return result
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
        if args.stale_before is not None and not _dispatch._has_stale_heartbeat(latest or {}, args.stale_before):
            print(f"job {job_id} no longer has a stale running heartbeat", file=sys.stderr)
            return 1
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
    error = _jobs.interrupt_job(
        store,
        registry,
        job,
        f"{message}: {args.reason}",
        db_path=db_path,
        registry_db=_registry.isolated_path(None, args.registry_db),
    )
    _record_intervention(store, job_id, "cancel", args.reason)
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
                    errors.append(
                        {"handle": _cloud.redact(str(handle)), "status": "failed", "reason": _cloud.redact(str(exc))}
                    )
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
            result = _stop_cloud_target(store, registry, provider, handle, owner_job)
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


def _stop_cloud_target(
    store: Any, registry: Any, provider: str, handle: str, owner_job: dict[str, Any] | None
) -> dict[str, Any]:
    """Use the common provider-stop path and update local tracking when confirmed."""
    from lazytools.code_bridge import _cloud_stop

    try:
        result = _cloud_stop.stop(provider, handle)
        if owner_job is not None and result.get("status") in ("stopped", "already finished"):
            _cloud.mark_interrupted(store, registry, str(owner_job["job_id"]), "cloud stop requested by operator")
            result["job_id"] = owner_job["job_id"]
        return result
    except Exception as exc:
        return {"provider": provider, "handle": handle, "status": "failed", "reason": _cloud.redact(str(exc))}


def _cancel_cloud(store: Any, registry: Any, job: dict[str, Any], reason: str) -> int:
    """Stop a cloud job at its provider, then report the local tracking result."""
    from lazytools.code_bridge import _cloud_stop

    job_id = str(job["job_id"])
    handle = job.get("cloud_handle")
    try:
        provider, ident = _cloud_stop.resolve_handle(str(handle))
    except Exception as exc:
        _cloud_print(f"cloud {handle or ''}: failed: {_cloud.redact(str(exc))}")
        _cloud_print(f"job {job_id} remains {job.get('status')} locally", err=True)
        return 1

    result = _cloud.redact_obj(_stop_cloud_target(store, registry, provider, ident, job))
    suffix = f": {result['reason']}" if result.get("reason") else ""
    _cloud_print(f"{provider} {ident}: {result['status']}{suffix}")
    if result.get("status") == "failed":
        _cloud_print(f"job {job_id} remains {job.get('status')} locally", err=True)
        return 1

    latest = _jobs.find_job(store, registry, job_id) or job
    if latest.get("status") == "interrupted":
        _cloud_print(f"[interrupted] job {job_id}\nprovider stop succeeded and local tracking is interrupted")
    else:
        _cloud_print(f"job {job_id} remains {latest.get('status')} locally; provider stop is not confirmed")
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
        job = _cloud.wait_job(
            lambda: _store.build_store(db_path), str(first_job["job_id"]), timeout=timeout, interval=args.interval
        )
        if job is not None and job.get("status") in _TERMINAL:
            owed_failed = False
            if _cloud_land.needs_finishing(job):
                # --land / --review-of: finishing the job (landing it, posting its review) is part of waiting for it.
                finish_store = _store.build_store(db_path)
                row = _cloud_land.process_job(
                    finish_store, _store.build_job_registry(finish_store), job, db_path=db_path
                )
                _cloud_print(f"{row['action']}: {row['detail']}", err=True)
                owed_failed = row["action"].endswith("-failed")
            code = _cmd_result(args)
            return code or (1 if owed_failed else 0)
        if job is None:
            print(f"job {args.job_id}: its record disappeared while waiting", file=sys.stderr)
            return 1
        errors = int(job.get("poll_errors_in_a_row") or 0)
        if errors:
            _cloud_print(
                f"job {args.job_id} still unknown after {errors} failed status poll(s); last error: "
                f"{job.get('last_poll_error')}",
                err=True,
            )
            return 4
        print(f"job {args.job_id} still {job.get('status')} after {timeout:.0f}s", file=sys.stderr)
        return 3

    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    pid = _detached_pid(db_path, args.job_id)
    registry_announced = False
    while True:
        store = _store.build_store(db_path)
        job = _jobs.find_job(store, _store.build_job_registry(store), args.job_id)
        if job is not None and not registry_announced:
            if registry_db := _registry.recorded_path(job) or _selected_registry_db(args):
                print(f"approval registry: {registry_db}", file=sys.stderr)
            registry_announced = True
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


def _cmd_wait_any(args: argparse.Namespace) -> int:
    """Wait for the next actionable state across a snapshot of jobs."""
    started_at = time.time()
    db_path = _db_path(args)
    try:
        events = {value.strip() for value in args.events.split(",") if value.strip()}
        unknown_events = events - _events.WAIT_EVENTS
        if unknown_events:
            raise ValueError(f"unknown --events value(s): {', '.join(sorted(unknown_events))}")
        if not events:
            raise ValueError("--events must contain at least one event")
        if args.all_active == bool(args.job_ids):
            raise ValueError("provide JOB_ID(s) or --all-active, but not both")
        if args.follow and not (args.all_active and args.loop):
            raise ValueError("--follow needs --all-active and --loop")
        if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout < 0):
            raise ValueError("--timeout must be a finite non-negative number")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    history: set[str] = set()
    if args.all_active and args.follow:
        # One snapshot: finished jobs are history, everything else is watched, so a job
        # launched while the stream starts cannot fall between two listings.
        snapshot = _jobs.list_jobs(store, all_jobs=True)
        history = {str(job["job_id"]) for job in snapshot if str(job.get("status") or "") in _TERMINAL}
        watched = [str(job["job_id"]) for job in snapshot if str(job["job_id"]) not in history]
    elif args.all_active:
        watched = [str(job["job_id"]) for job in _jobs.list_jobs(store, all_jobs=False)]
        if not watched:
            print("error: no active jobs", file=sys.stderr)
            return 2
    else:
        watched = []
        for ref in args.job_ids:
            job = _jobs.find_job(store, registry, ref)
            if job is None:
                print(f"error: no job found matching {ref!r}", file=sys.stderr)
                return 2
            job_id = str(job["job_id"])
            if job_id not in watched:
                watched.append(job_id)

    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    # The event computation is shared with the `bridge_events` MCP tool (`_events.poll`).
    state = _events.WatchState(watched=watched, history=history, follow=args.follow, started_at=started_at)
    while True:
        for event in _events.iter_events(
            state,
            db_path=db_path,
            registry_db=args.registry_db,
            kinds=events,
            finish=True,
            interval=args.interval,
            deadline=deadline,
        ):
            _print_json(event)
            if not args.loop:
                return 0
        if args.loop and not args.follow and len(state.seen_jobs) == len(state.watched):
            return 0
        if deadline is not None and time.monotonic() >= deadline:
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
    _record_intervention(store, str(job["job_id"]), "resync", args.reason)
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
        result = _cmd_land(args, store, registry, job, db_path)
        if result == 0 and not getattr(args, "_land_noop", False):
            _record_intervention(store, str(job["job_id"]), "land", args.reason)
        return result
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
    if outcome.get("changed"):
        _record_intervention(store, str(job["job_id"]), "apply", args.reason)
    if args.json:
        _cloud_json(outcome)
        return 0
    origin = f"{outcome['source_branch']} @ {outcome['base']}" if outcome.get("source_branch") else outcome["base"]
    _cloud_print(
        f"applied cloud job {outcome['job_id']} in {outcome['worktree']} (branch {outcome['branch']}, from {origin})"
    )
    for line in outcome["changed"]:
        _cloud_print(f"  {line}")
    if outcome["output"]:
        _cloud_print(outcome["output"])
    if outcome.get("rebased_from"):
        _cloud_print(
            f"note: --rebase: applied onto the current head of origin/{_cloud_base_branch(outcome)} "
            f"({outcome['base'][:12]}), not the task's base {outcome['rebased_from'][:12]}; review the result carefully"
        )
    _cloud_print(
        f'not pushed. Review, commit, then push only after approval: git -C "{outcome["worktree"]}" push -u origin {outcome["branch"]}'
        f" -- or land it in one step: lazytools-code-bridge apply {outcome['job_id']} --land"
    )
    return 0


def _cmd_land(args: argparse.Namespace, store: Any, registry: Any, job: dict[str, Any], db_path: Path | None) -> int:
    """``apply --land``: commit, push, then update the recorded PR or open a new one."""
    try:
        job = _cloud.refresh(store, registry, job)
        outcome = _cloud_land.land_job(
            store,
            registry,
            job,
            db_path=db_path,
            rebase_ok=True if args.rebase_ok else None,
            branch=args.branch,
            worktree=Path(args.worktree) if args.worktree else None,
            source=args.source,
        )
    except _cloud.CloudError as exc:
        _cloud_print(f"error: not landed: {exc}", err=True)
        return 1
    if outcome.get("already"):
        args._land_noop = True
    if args.json:
        _cloud_json(outcome)
        return 0
    if outcome.get("already"):
        _cloud_print(f"job {outcome['job_id']} was already landed")
    if outcome.get("note"):
        _cloud_print(outcome["note"])
    if outcome.get("pushed_sha"):
        _cloud_print(
            f"landed cloud job {outcome['job_id']}: pushed {outcome['branch']} @ {str(outcome['pushed_sha'])[:12]}"
        )
    if outcome.get("pr_url"):
        _cloud_print(f"PR: {outcome['pr_url']}")
    if outcome.get("force_added_ignored"):
        _cloud_print("force-added ignored cloud diff paths: " + ", ".join(outcome["force_added_ignored"]))
    if outcome.get("skipped_bridge_artifacts"):
        _cloud_print(
            "warning: skipped bridge-owned cloud diff paths: " + ", ".join(outcome["skipped_bridge_artifacts"])
        )
    return 0


def _cmd_cloud_sync(args: argparse.Namespace) -> int:
    """Reconcile every live cloud job: poll, land the finished ones that asked for it, record failures."""
    store = _store.build_store(_db_path(args))
    try:
        rows = _cloud_land.sync(_db_path(args))
    except (_cloud.CloudError, ValueError) as exc:
        _cloud_print(f"error: {exc}", err=True)
        return 1
    if args.manual:
        for row in rows:
            if row.get("action") != "none":
                _record_intervention(store, str(row["job_id"]), "cloud-sync", args.reason)
    failed = any(row["action"].endswith("-failed") for row in rows)
    if args.json:
        _cloud_json({"jobs": rows, "ok": not failed})
    elif not rows:
        print("no live cloud jobs to reconcile")
    else:
        for row in rows:
            _cloud_print(
                f"{str(row['job_id'])[:8]}  {row['engine']:<6}  {row['status']:<12}  {row['action']:<14}  {row['detail']}"
            )
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


def _cmd_cloud_preflight_init(args: argparse.Namespace) -> int:
    try:
        root = _cloud.repo_root(Path(args.repo).expanduser().resolve())
        slug = _cloud.repo_slug(root)
        config = _cloud.load_config()
        env_id = config.envs.get("codex", {}).get(slug.lower())
        if not env_id:
            raise ValueError(f"no configured Codex cloud environment id for {slug} in {config.path}")
        path = _cloud_preflight.init_manifest(root=root, env_id=env_id, network=args.network == "on")
    except (OSError, ValueError, _cloud.CloudError) as exc:
        _cloud_print(f"error: {exc}", err=True)
        return 2
    _cloud_print(str(path))
    return 0


def _preflight_checkouts(mapped: dict[str, str]) -> dict[str, Path]:
    """Locate local checkouts by their origin, including linked worktrees."""
    configured = os.environ.get("LAZYTOOLS_CODE_ROOT")
    workspace = Path(configured).expanduser() if configured else _concurrency.load_workspace().root
    if workspace is None:
        cwd = Path.cwd()
        try:
            workspace = _cloud.repo_root(cwd).parent
        except _cloud.CloudError:
            workspace = cwd
    found: dict[str, Path] = {}
    for directory, dirs, files in os.walk(workspace.resolve()):
        if ".git" in dirs or ".git" in files:
            dirs.clear()
            root = Path(directory)
            try:
                slug = _cloud.repo_slug(root).lower()
            except _cloud.CloudError:
                continue
            if slug in mapped:
                found.setdefault(slug, root)
            if found.keys() == mapped.keys():
                break
        else:
            dirs[:] = sorted(name for name in dirs if not name.startswith("."))
    return found


def _probe_cloud_preflight(root: Path, slug: str, config: _cloud.CloudConfig, args: argparse.Namespace) -> None:
    requirements = _cloud_preflight.project_requirements(root, allow_missing=True)
    expected = None if args.network is None else args.network == "on"
    db_path = _db_path(args)
    job = _cloud.submit_job(
        engine="codex", cwd=root, task=_cloud_preflight.Probe(requirements, expected),
        config=config, db_path=db_path, title="Cloud preflight probe",
    )
    job_id = str(job["job_id"])
    _cloud_print(f"{slug}: waiting for probe {job_id}")
    sys.stdout.flush()
    completed = _cloud.wait_job(
        lambda: _store.build_store(db_path), job_id, timeout=_cloud.DEFAULT_CLOUD_WAIT_TIMEOUT, interval=5.0,
    )
    if completed is None or completed.get("status") not in _cloud.TERMINAL_STATUSES:
        raise TimeoutError(f"probe {job_id} did not reach a terminal state; check its status before retrying")
    if completed.get("status") != "done":
        raise _cloud.CloudError(str(completed.get("error") or f"probe {job_id} ended with {completed['status']}"))

    # The CLI may show task metadata in status without the agent's final message.
    output = str(completed.get("cloud_message") or completed.get("result") or "")
    if "```" not in output:
        store = _store.build_store(db_path)
        reported, _ = _cloud.capture_report(store, _store.build_job_registry(store), completed)
        output = str(reported.get("report") or "")
    evidence = _cloud_preflight.parse_probe_output(output, requirements)
    path = _cloud_preflight.write_probe_manifest(root=root, env_id=str(job["env_id"]), job_id=job_id, evidence=evidence)
    network = "on" if evidence.network else "off"
    _cloud_print(f"{slug}: wrote {path} (Python {evidence.python_version}, network {network})")
    if expected is not None and expected != evidence.network:
        _cloud_print(f"{slug}: expected network {args.network}, observed {network}", err=True)
    for field, results in (("imports", evidence.imports), ("repositories", evidence.repositories)):
        gaps = sorted(name for name, passed in results.items() if not passed)
        if gaps:
            _cloud_print(f"{slug}: unavailable {field}: {', '.join(gaps)}")


def _cmd_cloud_preflight_probe(args: argparse.Namespace) -> int:
    try:
        config = _cloud.load_config()
        mapped = config.envs.get("codex", {})
        if args.all:
            if not mapped:
                raise ValueError(f"no Codex cloud environments are mapped in {config.path}")
            found = _preflight_checkouts(mapped)
            targets = {slug: found.get(slug) for slug in sorted(mapped)}
        else:
            root = _cloud.repo_root(Path(args.repo).expanduser().resolve())
            targets = {_cloud.repo_slug(root).lower(): root}
    except (OSError, ValueError, _cloud.CloudError, subprocess.TimeoutExpired) as exc:
        _cloud_print(f"error: {exc}", err=True)
        return 2

    failed = False
    for slug, checkout in targets.items():
        if checkout is None:
            _cloud_print(f"error: {slug}: no local checkout found; set LAZYTOOLS_CODE_ROOT or use --repo", err=True)
            failed = True
            continue
        try:
            _probe_cloud_preflight(checkout, slug, config, args)
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            _cloud_print(f"error: {slug}: {exc}", err=True)
            return 1
        except (OSError, ValueError, _cloud.CloudError) as exc:
            _cloud_print(f"error: {slug}: {exc}", err=True)
            failed = True
    return int(failed)


def _cmd_cloud_setup(args: argparse.Namespace) -> int:
    try:
        config = _cloud.load_config()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = _cloud_envs.check_claude_setup(config)
    if args.json:
        _cloud_json(report)
    else:
        _cloud_print(_cloud_envs.format_claude_setup(report))
    return 0 if report["ok"] else 1


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

    store = _ticket_job_store(args)
    tickets = _pending_tickets(args, store)
    if args.json:
        _print_json(
            [
                {
                    "approval_id": t.approval_id,
                    "job_id": _registry.job_ref(t),
                    "gist": ticket_gist(t.prompt),
                    **_request_detail(t.prompt),
                    "kind": t.kind,
                    "created_at": t.created_at.isoformat(),
                    "expires_at": t.expires_at.isoformat(),
                    **(
                        {
                            "task_id": t.task_id,
                            "action_digest": t.request.digest,
                            "policy_revision": t.request.policy_revision,
                        }
                        if t.request is not None
                        else {}
                    ),
                }
                for t in tickets
            ]
        )
        return 0
    if not tickets:
        print("no pending approvals")
        return 0
    for t in tickets:
        print(f"{t.approval_id}  job={_registry.job_ref(t)[:8]}  {ticket_gist(t.prompt, max_len=120)}")
        for key, value in _request_detail(t.prompt).items():
            print(f"  {key}: {value}")
        print(f"  created {t.created_at.isoformat()}  expires {t.expires_at.isoformat()}")
    return 0


def _cmd_decide(args: argparse.Namespace, action: Literal["approve", "reject"]) -> int:
    from lazytools.projects.task_store import TaskServiceError

    store = _ticket_job_store(args)
    verdict: Literal["approved", "rejected"] = "approved" if action == "approve" else "rejected"
    for registry, job_ids in _operator_registries(args, store):
        ticket = registry.inspect(args.ticket_id)
        if ticket is None:
            continue
        job_id = _registry.job_ref(ticket)
        if job_ids is not None and job_id not in job_ids:
            continue
        reason = args.reason
        # The bridge does not record who launched a job, so every job counts as the CEO's own.
        ceo = bool(getattr(args, "ceo", False))
        try:
            if not registry.decide(ticket, verdict, args.actor, reason, ceo=ceo):
                break
            if ceo:
                decided = registry.inspect(args.ticket_id)
                reason = decided.reason if decided is not None else reason
        except (OSError, ValueError, TaskServiceError) as exc:
            print(f"could not {action} {args.ticket_id!r}: {exc}", file=sys.stderr)
            return 1
        if _store.read_meta(store, job_id) is not None:
            _record_intervention(store, job_id, action, reason)
        print(f"{verdict} {args.ticket_id}")
        return 0
    print(f"could not {action} {args.ticket_id!r}: not pending, already resolved, or expired", file=sys.stderr)
    return 1


_CEO_HELP = (
    "Decide as the CEO: approve only a class the owner's autonomy policy delegates to the CEO in the "
    "current mode, and record actor, mode and class with the reason."
)


def _cmd_approve(args: argparse.Namespace) -> int:
    return _cmd_decide(args, "approve")


def _cmd_reject(args: argparse.Namespace) -> int:
    return _cmd_decide(args, "reject")


def _cmd_init_approver_credential(args: argparse.Namespace) -> int:
    registry = _registry.Registry(_ticket_job_store(args), _selected_registry_db(args))
    try:
        created = registry.initialize_credential(replace=args.replace)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"could not initialize owner approver credential: {exc}", file=sys.stderr)
        return 1
    print("Owner approver credential initialized." if created else "Owner approver credential already initialized.")
    return 0


def _cmd_provision_automatic_cards(args: argparse.Namespace) -> int:
    from lazytools.projects.task_approvals import ProjectTaskApprovals
    from lazytools.projects.task_service import ProjectTaskService
    from lazytools.projects.task_store import TaskServiceError

    try:
        path = _selected_registry_db(args) or _registry._registry_path()
        if path is None:
            raise ValueError("A persistent projects store is required")
        credential = _credentials.load(path)
        ProjectTaskApprovals(ProjectTaskService(str(path))).provision_automatic_cards(
            args.project_id, credential=credential,
        )
    except (OSError, ValueError, TaskServiceError) as exc:
        print(f"could not provision automatic approval cards: {exc}", file=sys.stderr)
        return 1
    print(f"Automatic approval cards provisioned for {args.project_id}.")
    return 0


def _cmd_provision_coder(args: argparse.Namespace) -> int:
    from lazytools.projects.task_store import TaskServiceError

    try:
        path = _selected_registry_db(args) or _registry._registry_path()
        if path is None:
            raise ValueError("A persistent approval registry is required")
        created = _coder.provision(path)
    except (OSError, ValueError, TaskServiceError) as exc:
        print(f"could not provision coder cards: {exc}", file=sys.stderr)
        return 1
    print("Coder hand provisioned." if created else "Coder hand already provisioned.")
    return 0


def _cmd_configure_autonomy(args: argparse.Namespace) -> int:
    from lazytools.projects.task_store import TaskServiceError

    if args.show and args.preset is not None:
        print("--preset requires --mode", file=sys.stderr)
        return 2
    try:
        path = _selected_registry_db(args) or _registry._registry_path()
        if path is None:
            raise ValueError("A persistent approval registry is required")
        selection = _coder.show(path) if args.show else _coder.configure(path, args.mode, args.preset)
    except (OSError, ValueError, TaskServiceError) as exc:
        print(f"could not read or configure autonomy: {exc}", file=sys.stderr)
        return 1
    classes = sorted({card.action_class for card in selection.cards})
    print(f"Mode: {selection.mode}")
    print(f"Preset: {selection.preset}")
    print(f"Delegated classes: {', '.join(classes) if classes else 'none'}")
    return 0


def _cmd_autonomy(args: argparse.Namespace) -> int:
    try:
        if args.week:
            if args.until:
                raise ValueError("--until cannot be combined with --week")
            since, until = _autonomy.week_bounds(args.week)
        else:
            today_utc = datetime.now(UTC).date()
            since = date.fromisoformat(args.since) if args.since else today_utc - timedelta(days=7)
            until = date.fromisoformat(args.until) if args.until else today_utc + timedelta(days=1)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if since >= until:
        print("error: --since must be before --until", file=sys.stderr)
        return 2
    payload = _autonomy.report(_store.build_store(_db_path(args)), since=since, until=until)
    if args.json:
        _print_json(payload)
    else:
        percent = "n/a" if payload["autonomy_percent"] is None else f"{payload['autonomy_percent']:.1f}%"
        print(
            f"Autonomy: {payload['autonomous_jobs']}/{payload['finished_jobs']} finished jobs ({percent}); target 90.0%"
        )
        print("Intervention causes:")
        if not payload["intervention_causes"]:
            print("  none")
        for kind, count in payload["intervention_causes"].items():
            print(f"  {kind}: {count}")
        print("Card approvals by class:")
        if not payload["card_approvals_by_class"]:
            print("  none")
        for kind, count in payload["card_approvals_by_class"].items():
            print(f"  {kind}: {count}")
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


def _cmd_watch(args: argparse.Namespace) -> int:
    try:
        pulls = [_watch.parse_pull(value) for value in args.pulls]
        if not math.isfinite(args.interval) or args.interval <= 0:
            raise ValueError("--interval must be a positive finite number")
        until = None
        if args.until:
            try:
                clock = datetime.strptime(args.until, "%H:%M").time()
            except ValueError as exc:
                raise ValueError("--until must use 24-hour HH:MM format") from exc
            until = datetime.combine(datetime.now().astimezone().date(), clock).astimezone()
        store = _store.build_store(_db_path(args))

        def emit(event: _watch.WatchEvent) -> None:
            if args.json:
                _print_json(event.as_dict())
            else:
                print(event.time, event.pull, event.kind, event.detail, flush=True)

        return _watch.run_watch(pulls, store, interval=args.interval, until=until, emit=emit,
                                request_review=args.request_review, no_ci=args.no_ci)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


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
    from lazytools.code_bridge._navigator import add_navigate_parser

    add_navigate_parser(sub)

    dispatch_p = sub.add_parser("dispatch", help="Dispatch every ready task in a project.")
    dispatch_p.add_argument("--project", required=True, help="Projects board identifier.")
    dispatch_p.add_argument("--parallel", type=int, default=1, metavar="N")
    dispatch_p.add_argument("--once", action="store_true")
    dispatch_p.add_argument("--dry-run", action="store_true")
    dispatch_p.add_argument("--repo-root", action="append", default=[], metavar="SLUG=PATH",
                            help="Map a task repository slug to its local checkout (repeatable).")
    dispatch_p.add_argument("--worktree-root", default=None, metavar="PATH",
                            help="Root for retained per-task git worktrees.")
    _add_registry_option(dispatch_p)
    dispatch_p.add_argument("--knowledge-db", type=Path, metavar="PATH",
                            help="Store path for lessons from previous dispatches.")
    dispatch_p.add_argument("--lease-minutes", type=int, default=60, metavar="N",
                            help="Claim lease per task; a job running longer is stopped (default 60).")
    dispatch_p.add_argument("--stale-minutes", type=int, default=_dispatch.DEFAULT_STALE_MINUTES, metavar="N",
                            help="Stop running jobs with no engine activity for this many minutes (default 10).")
    dispatch_p.add_argument("--launch-mode", choices=("auto", "governed", "legacy"), default=None,
                            help="Passed to every task's run (default: the run default).")
    dispatch_p.add_argument("--json", action="store_true",
                            help="Print one JSON event per line with the keys wait-any events carry.")
    dispatch_p.add_argument("--land", action="store_true",
                            help="Push each published task branch to origin and open its PR (or update an open one).")
    dispatch_p.set_defaults(func=_dispatch.dispatch)

    watch_p = sub.add_parser("watch", help="Print new events for one or more GitHub pull requests.")
    watch_p.add_argument("pulls", nargs="+", metavar="owner/repo#pr")
    watch_p.add_argument(
        "--until", default=None, metavar="HH:MM", help="Stop successfully at this local 24-hour clock time."
    )
    watch_p.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Polling interval in seconds (default 60; clamped to the configured minimum).",
    )
    watch_p.add_argument("--json", action="store_true", help="Print one JSON object per event.")
    watch_p.add_argument("--request-review", default=None, metavar="TEXT",
                         help="Post TEXT (e.g. '@codex review') once per new head the review bot left unreviewed "
                              "for two polls. Off by default; the only write the watcher makes.")
    watch_p.add_argument("--no-ci", action="store_true",
                         help="The repository has no CI by policy: emit `ready` on a clean or non-blocking review "
                              "and confirmed mergeability alone. CI that does report must still be green.")
    _add_db_option(watch_p)
    watch_p.set_defaults(func=_cmd_watch)

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
    run_p.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="--where cloud only: work on pull request N's head branch; the job records the PR.",
    )
    run_p.add_argument(
        "--open-pr",
        action="store_true",
        help="--where cloud only: when landed, push a new branch and open a PR against --branch.",
    )
    run_p.add_argument(
        "--title",
        default=None,
        help="--where cloud only: explicit PR, landing commit, and squash subject (default: # Goal text).",
    )
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
        "--branch",
        default=None,
        help="--where cloud only: the base branch the cloud agent starts from; it must exist on origin "
        "(default: current).",
    )
    run_p.add_argument(
        "--routine",
        default=None,
        help="--where cloud --engine claude only: fire this [cloud.claude.routines] entry (its API trigger token is "
        "read from the env var the config names) instead of running `claude --cloud`. Without it, --model picks "
        "the routine that declares that model, else [cloud.claude] default_routine.",
    )
    run_p.add_argument(
        "--allow-cloud",
        action="store_true",
        help="--where cloud only, required: acknowledge that the cloud agent's commands run outside the local "
        "approval gate (only its diff comes back, through `apply`).",
    )
    run_p.add_argument(
        "--network",
        choices=("on", "off"),
        default=None,
        help="Requested task network access. Defaults to off for core profiles and cloud; the legacy writer defaults to on.",
    )
    _add_routing_options(run_p)
    run_p.add_argument("--cwd", required=True, help="Repository (or subdirectory) to work in.")
    run_p.add_argument(
        "--task", default=None, help="Task text, or @path/to/file (required, except for a cloud --review-of)."
    )
    run_p.add_argument("--session", default=None, help="Durable session alias to create/resume.")
    run_p.add_argument("--model", default=None)
    run_p.add_argument("--effort", default=None)
    run_p.add_argument(
        "--timeout",
        type=float,
        default=_engines.DEFAULT_TIMEOUT,
        help=f"Per-turn timeout in seconds, greater than 0 (default: {_engines.DEFAULT_TIMEOUT:g}).",
    )
    run_p.add_argument(
        "--max-continuations",
        type=int,
        default=_jobs.DEFAULT_MAX_CONTINUATIONS,
        help="Automatic continuations after a timeout with committed/pushed progress (0-10, default: 3).",
    )
    run_p.add_argument(
        "--max-wall",
        type=float,
        default=None,
        help="Total job wall-clock cap in seconds, including approvals (default: unlimited).",
    )
    run_p.add_argument(
        "--profile",
        choices=_launch.PROFILES,
        default="default",
        help="Local core policy profile. Legacy default maps to edit-and-run, and review maps to read.",
    )
    run_p.add_argument(
        "--launch-mode",
        choices=_launch.MODES,
        default="auto",
        help="Governed requires matching release evidence. Legacy explicitly allows an ungoverned launch.",
    )
    run_p.add_argument("--task-id", help="Projects task ID: for a local launch paired with --claim-token; "
                       "a cloud job only records it.")
    run_p.add_argument("--claim-token", help="Fence token returned by the projects task claim.")
    run_p.add_argument("--intentional-review", action="store_true", help=argparse.SUPPRESS)
    run_p.add_argument("--detached-binding", action="store_true", help=argparse.SUPPRESS)
    run_p.add_argument(
        "--root",
        default=None,
        help="Confinement root for --cwd (default: $LAZYTOOLS_CODE_ROOT, root in code-bridge.toml, then cwd).",
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
    run_p.add_argument(
        "--require-fresh",
        action="store_true",
        help="Refuse to launch (exit 2, no job record) when the current branch is behind its upstream. "
        "Without it, a stderr warning with the commit count is printed and the job still launches.",
    )
    run_p.add_argument(
        "--no-freshness-check",
        action="store_true",
        help="Skip the pre-launch fetch and the behind-upstream check.",
    )
    run_p.add_argument("--brief-checked", action="store_true", help=argparse.SUPPRESS)
    run_p.add_argument("--job-id", default=None, help=argparse.SUPPRESS)
    run_p.add_argument("--routing-record", default=None, help=argparse.SUPPRESS)
    _add_registry_option(run_p)
    run_p.set_defaults(func=_cmd_run)

    route_p = sub.add_parser("route", help="Explain a quota-aware pick; launch nothing.")
    route_p.add_argument("--engine", choices=list(_jobs.ENGINES))
    route_p.add_argument(
        "--where",
        choices=list(_cloud_place.WHERE_CHOICES),
        default=None,
        help="Also show where the job would run: auto explains its local-vs-cloud decision.",
    )
    _add_routing_options(route_p, required=True)
    route_p.add_argument("--cwd", default=".", help="Repository used to resolve session history (default: cwd).")
    route_p.add_argument("--root", default=None, help="Confinement root for placement (same rules as run).")
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

    capabilities_p = sub.add_parser("capabilities", help="List or search the capabilities catalog.")
    capabilities_p.add_argument(
        "query", nargs="?", default="", metavar="QUERY", help="Case-insensitive substring of a capability or call."
    )
    capabilities_p.set_defaults(func=_cmd_capabilities)

    wait_p = sub.add_parser("wait", help="Block until a job ends (or its process dies), then print its outcome.")
    wait_p.add_argument("job_id")
    wait_p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Give up after this many seconds (exit 3). Cloud jobs default to 7200.",
    )
    wait_p.add_argument("--interval", type=float, default=5.0, help=argparse.SUPPRESS)
    wait_p.add_argument("--json", action="store_true")
    _add_registry_option(wait_p)
    wait_p.set_defaults(func=_cmd_wait)

    wait_any_p = sub.add_parser("wait-any", help="Block until an event occurs on any watched job.")
    wait_any_p.add_argument("job_ids", nargs="*", metavar="JOB_ID")
    wait_any_p.add_argument("--all-active", action="store_true", help="Watch all active jobs (a snapshot; with --loop, also jobs started later).")
    wait_any_p.add_argument("--timeout", type=float, default=None, help="Give up after this many seconds (exit 3).")
    wait_any_p.add_argument(
        "--events", default="done,failed,approval,landed,interrupted", help="Comma-separated events to return."
    )
    wait_any_p.add_argument("--loop", action="store_true", help="Print events until every watched job is terminal.")
    wait_any_p.add_argument(
        "--follow", action="store_true",
        help="With --all-active and --loop: also watch jobs that start later and keep running when the current "
             "jobs end (the orchestrator's single event stream; stop with --timeout or Ctrl-C).",
    )
    wait_any_p.add_argument(
        "--json", action="store_true", help="Emit the event as one JSON object (the default format)."
    )
    wait_any_p.add_argument("--interval", type=float, default=0.25, help=argparse.SUPPRESS)
    _add_registry_option(wait_any_p)
    wait_any_p.set_defaults(func=_cmd_wait_any)

    resync_p = sub.add_parser("resync", help="Re-check a cloud job that an older bridge failed after a poll error.")
    resync_p.add_argument("job_id")
    resync_p.add_argument("--reason", default=None, help="Optional reason for the manual intervention.")
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
    _add_registry_option(status_p)
    status_p.set_defaults(func=_cmd_status)

    cancel_p = sub.add_parser(
        "cancel", help="Stop a job's process tree, record workspace evidence, and reject its open approvals."
    )
    cancel_p.add_argument("job_id", help="Full job id or unique prefix.")
    cancel_p.add_argument("--reason", default="requested cancellation", help="Reason recorded with the interruption.")
    cancel_p.add_argument("--stale-before", type=float, help=argparse.SUPPRESS)
    _add_registry_option(cancel_p)
    cancel_p.set_defaults(func=_cmd_cancel)

    stop_p = sub.add_parser(
        "cloud-stop", help="Stop a Codex Cloud task or request that a Claude Code cloud session stop."
    )
    stop_target = stop_p.add_mutually_exclusive_group(required=True)
    stop_target.add_argument("target", nargs="?", help="Task URL, task/session id, or bridge job id.")
    stop_target.add_argument(
        "--all-running", action="store_true", help="Stop account-wide Codex tasks and running bridge cloud jobs."
    )
    stop_p.add_argument("--dry-run", action="store_true", help="List targets without making provider requests.")
    stop_p.add_argument("--json", action="store_true", help="Emit machine-readable results for Agent Radar.")
    _add_db_option(stop_p)
    stop_p.set_defaults(func=_cmd_cloud_stop)

    result_p = sub.add_parser(
        "result", help="Show one job's result or error, including workspace evidence for unfinished jobs."
    )
    result_p.add_argument("job_id")
    result_p.add_argument("--json", action="store_true")
    _add_registry_option(result_p)
    result_p.set_defaults(func=_cmd_result)

    apply_p = sub.add_parser(
        "apply",
        help="Apply a finished cloud job's diff in a NEW git worktree on a NEW branch (never your checkout); does not push.",
    )
    apply_p.add_argument("job_id")
    apply_p.add_argument("--branch", default=None, help="New branch name (default: cloud/<job id prefix>).")
    apply_p.add_argument(
        "--worktree", default=None, help="New worktree path (default: beside the Store, cloud-worktrees/<job>)."
    )
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
    apply_p.add_argument("--reason", default=None, help="Optional reason for the manual intervention.")
    _add_db_option(apply_p)
    apply_p.set_defaults(func=_cmd_apply)

    sync_p = sub.add_parser(
        "cloud-sync",
        help="Reconcile every live cloud job: poll the provider, land finished jobs submitted with --land, post "
        "finished reviews, record failures with the provider's reason. Safe to run from a scheduler.",
    )
    sync_p.add_argument("--json", action="store_true")
    sync_p.add_argument("--reason", default=None, help="Optional reason for the manual intervention.")
    sync_mode = sync_p.add_mutually_exclusive_group()
    sync_mode.add_argument(
        "--manual",
        action="store_true",
        help="Record successful reconciliation work as an operator intervention.",
    )
    sync_mode.add_argument(
        "--scheduled",
        action="store_true",
        help="Scheduler mode (the default): do not record an operator intervention.",
    )
    _add_db_option(sync_p)
    sync_p.set_defaults(func=_cmd_cloud_sync)

    envs_p = sub.add_parser(
        "cloud-envs", help="List Codex Cloud environments (and their linked repo) and the Claude cloud environment(s)."
    )
    envs_p.add_argument("--cwd", default=".", help="Repository to check for a mapped Codex environment (default: cwd).")
    envs_p.add_argument("--no-probe", action="store_true", help="Do not call `codex cloud list`; show the config only.")
    envs_p.add_argument("--json", action="store_true")
    envs_p.set_defaults(func=_cmd_cloud_envs)

    preflight_p = sub.add_parser("cloud-preflight", help="Manage repository-owned cloud eligibility evidence.")
    preflight_sub = preflight_p.add_subparsers(dest="cloud_preflight_command", required=True)
    preflight_init_p = preflight_sub.add_parser("init", help="Write a starter manifest from pyproject dependencies.")
    preflight_init_p.add_argument("--repo", required=True, help="Repository whose manifest should be created.")
    preflight_init_p.add_argument(
        "--network",
        choices=("on", "off"),
        default="off",
        help="Attest that the configured environment network is on or off (default: off).",
    )
    preflight_init_p.set_defaults(func=_cmd_cloud_preflight_init)

    preflight_probe_p = preflight_sub.add_parser("probe", help="Measure Codex cloud capabilities and write a manifest.")
    probe_target = preflight_probe_p.add_mutually_exclusive_group(required=True)
    probe_target.add_argument("--repo", help="Repository whose mapped environment should be probed.")
    probe_target.add_argument("--all", action="store_true", help="Probe every mapped Codex environment in sequence.")
    preflight_probe_p.add_argument("--network", choices=("on", "off"), help="Expected PyPI reachability for the probe.")
    _add_db_option(preflight_probe_p)
    preflight_probe_p.set_defaults(func=_cmd_cloud_preflight_probe)

    setup_p = sub.add_parser(
        "cloud-setup",
        help="Check the Claude cloud routine setup locally (config, token variables, a model per routine); "
        "no network call, token values are never printed. Exit 1 when something is missing.",
    )
    setup_p.add_argument("provider", choices=["claude"])
    setup_p.add_argument("--check", action="store_true", help="Run the check (the default and only action).")
    setup_p.add_argument("--json", action="store_true")
    setup_p.set_defaults(func=_cmd_cloud_setup)

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

    publish_p = sub.add_parser("publish", help="Publish a reviewed commit as a draft pull request.")
    publish_p.add_argument("--task", required=True)
    publish_p.add_argument("--repo", required=True, type=Path)
    publish_p.add_argument("--branch", required=True)
    publish_p.add_argument("--base", default="main")
    publish_ticket = publish_p.add_mutually_exclusive_group()
    publish_ticket.add_argument("--ticket", metavar="ID", help="Use this typed publication ticket.")
    publish_ticket.add_argument(
        "--retry-ticket", metavar="ID",
        help="Request fresh approval after push_failed, pr_create_failed, pr_head_mismatch, or pr_head_unavailable.",
    )
    publish_p.add_argument("--request-only", action="store_true", help="File or inspect the ticket without publishing.")
    _add_registry_option(publish_p)
    publish_p.set_defaults(func=_cmd_publish)

    verify_p = sub.add_parser("verify-receipt", help="Verify a signed publication receipt.")
    verify_p.add_argument("receipt", metavar="receipt_id|file")
    verify_p.add_argument("--live", action="store_true")
    verify_p.add_argument("--public-key", type=Path)
    verify_p.set_defaults(func=_cmd_verify_receipt)

    pending_p = sub.add_parser("pending", help="List open approval tickets.")
    pending_p.add_argument("--json", action="store_true")
    _add_registry_option(pending_p)
    pending_p.set_defaults(func=_cmd_pending)

    credential_p = sub.add_parser("approver-credential", help="Manage the local owner approver credential.")
    credential_sub = credential_p.add_subparsers(dest="credential_command", required=True)
    credential_init = credential_sub.add_parser("init", help="Initialize the owner credential for a registry.")
    credential_init.add_argument("--replace", action="store_true", help="Replace an invalid or obsolete credential.")
    _add_registry_option(credential_init)
    credential_init.set_defaults(func=_cmd_init_approver_credential)

    automatic_cards = credential_sub.add_parser(
        "provision-automatic", help="Provision the owner's automatic approval cards for a project.",
    )
    automatic_cards.add_argument("project_id")
    _add_registry_option(automatic_cards)
    automatic_cards.set_defaults(func=_cmd_provision_automatic_cards)

    coder_cards = credential_sub.add_parser("provision-coder", help="Provision the owner's coder hand for this registry.")
    _add_registry_option(coder_cards)
    coder_cards.set_defaults(func=_cmd_provision_coder)

    autonomy_config = credential_sub.add_parser("autonomy", help="Read or set the owner-delegated coder hand.")
    autonomy_operation = autonomy_config.add_mutually_exclusive_group(required=True)
    autonomy_operation.add_argument("--show", action="store_true")
    autonomy_operation.add_argument("--mode", choices=_coder.MODES)
    autonomy_config.add_argument("--preset", help="Setup preset. Defaults to the current preset or with-production.")
    _add_registry_option(autonomy_config)
    autonomy_config.set_defaults(func=_cmd_configure_autonomy)

    approve_p = sub.add_parser("approve", help="Approve a pending ticket.")
    approve_p.add_argument("ticket_id")
    approve_p.add_argument("--actor", default="operator", help="Actor label for historical prompt-only tickets.")
    approve_p.add_argument("--reason", default=None, help="Optional reason for the manual intervention.")
    approve_p.add_argument("--ceo", action="store_true", help=_CEO_HELP)
    _add_registry_option(approve_p)
    approve_p.set_defaults(func=_cmd_approve)

    reject_p = sub.add_parser("reject", help="Reject a pending ticket.")
    reject_p.add_argument("ticket_id")
    reject_p.add_argument("--reason", required=True)
    reject_p.add_argument("--actor", default="operator", help="Actor label for historical prompt-only tickets.")
    reject_p.add_argument("--ceo", action="store_true", help=_CEO_HELP)
    _add_registry_option(reject_p)
    reject_p.set_defaults(func=_cmd_reject)

    autonomy_p = sub.add_parser("autonomy", help="Report the share of finished jobs needing no manual intervention.")
    range_group = autonomy_p.add_mutually_exclusive_group()
    range_group.add_argument("--week", help="ISO week (YYYY-Www).")
    range_group.add_argument("--since", help="First UTC date to include (YYYY-MM-DD).")
    autonomy_p.add_argument(
        "--until",
        help="Exclusive UTC end date (YYYY-MM-DD): that date is not included; defaults to tomorrow in UTC.",
    )
    autonomy_p.add_argument("--json", action="store_true")
    _add_db_option(autonomy_p)
    autonomy_p.set_defaults(func=_cmd_autonomy)

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


def _cmd_publish(args: argparse.Namespace) -> int:
    from lazytools.code_bridge import _gh, _publication, _receipts
    from lazytools.connectors.projects.tools import _resolve_store_db
    from lazytools.projects.task_service import ProjectTaskService
    from lazytools.projects.task_store import TaskServiceError, migrate_publication_receipts

    try:
        service = ProjectTaskService(_resolve_store_db(None))
        migrate_publication_receipts(service.path)
        attempt = _publication.prepare(service, args.task, args.repo.resolve(), args.branch, args.base)
        store = _ticket_job_store(args)
        registry = _registry.Registry(store, _selected_registry_db(args))
        key = _receipts.load_issuer()
        receipt = _receipts.template(attempt, store, key)
        policy = receipt.writer.policy
        job = attempt.review.writer
        result = _publication.run(
            service,
            registry,
            attempt,
            policy_revision=policy.revision or "publication-v1",
            policy_digest=policy.digest,
            ticket_id=args.ticket,
            request_only=args.request_only or args.retry_ticket is not None,
            retry_id=args.retry_ticket,
        )
        receipt = _receipts.finish(
            receipt,
            result,
            key,
            projects_db=service.path,
            job_db=_db_path(args),
            job_ref=job.job_ref if job else None,
        )
    except (ImportError, OSError, subprocess.SubprocessError, TaskServiceError, ValueError, _cloud.CloudError) as exc:
        print(f"Publication unavailable: {_cloud.redact(str(exc))}", file=sys.stderr)
        return 1
    if ticket := receipt.approval.ticket_id:
        print(f"approval {ticket} {attempt.repository} {attempt.head_branch} {attempt.head_sha}")
    print(
        f"{receipt.outcome.status} {receipt.receipt_id} {receipt.outcome.refusal_reason or receipt.publication.pr_url}"
    )
    if result.reason is None and result.pr_number is not None:
        try:
            _gh.pr_comment(
                f"github.com/{attempt.repository}",
                result.pr_number,
                f"Publication receipt `{receipt.receipt_id}`\n\n"
                f"Issuer fingerprint `{receipt.issuer.public_key_fingerprint}`",
            )
        except (_cloud.CloudError, OSError, subprocess.SubprocessError):
            print("The publication receipt was stored. The PR comment could not be posted.", file=sys.stderr)
    return 0 if receipt.outcome.status == "published" else 1


def _cmd_verify_receipt(args: argparse.Namespace) -> int:
    from lazytools.code_bridge import _receipts
    from lazytools.connectors.projects.tools import _resolve_store_db
    from lazytools.projects.task_service import ProjectTaskService
    from lazytools.projects.task_store import TaskServiceError

    try:
        path = ProjectTaskService(_resolve_store_db(None)).path
        raw = _receipts.read_source(path, args.receipt)
        result = _receipts.verify_json(raw, live=args.live, public_key=args.public_key)
    except (ImportError, OSError, TaskServiceError) as exc:
        print(f"Verification unavailable: {exc}", file=sys.stderr)
        return 1
    print("valid" if result["valid"] else result["reason"])
    return 0 if result["valid"] else 1


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
    if args.command == "run":
        from lazytools.code_bridge._credentials import scrub_job_environment

        with scrub_job_environment():
            return args.func(args)
    return args.func(args)


__all__ = ["build_parser", "main"]
