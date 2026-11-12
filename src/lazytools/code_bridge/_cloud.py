"""Cloud dispatch: hand a brief to a CLOUD coding agent instead of a local engine.

A local job runs Codex or Claude Code on the owner's machine, where it
competes with production jobs during production windows. A cloud job runs on
the provider's infrastructure: this module only submits the brief, records the
returned task handle, and later polls / fetches / applies the result. Nothing
heavy runs locally, and no local process stays alive for the job.

Providers
---------
Each cloud engine is a :class:`CloudProvider`. Only Codex Cloud is implemented
(:class:`CodexCloud`, driving the official ``codex cloud`` CLI). Claude cloud
routines are an explicit extension point (:class:`ClaudeRoutines`): it is
registered so ``--where cloud --engine claude`` fails with a pointer to the
docs rather than an argparse error, but every method raises
:class:`CloudNotImplemented`. See ``docs/code-bridge.md`` ("Cloud jobs").

Configuration (``~/.lazytools/code-bridge.toml``, same file as ``[concurrency]``)::

    [cloud]
    concurrency = 4          # live cloud jobs at once, all providers; 0 = unlimited

    [cloud.codex.envs]
    "selvaz/LazyTools" = "env_0123abcd"   # git remote 'owner/name' -> Codex Cloud env id

Concurrency
-----------
Cloud jobs never take a local engine slot (``_concurrency.EngineSlot``): they
use no local CPU, so they must not block -- or be blocked by -- a local Codex
job. They have their own limit, counted from the job Store (records with
``where == "cloud"`` that are not terminal), refreshed against the provider
before a submit is refused, and admitted under the same machine-wide guard
lock the engine slots use, so two submits cannot both take the last place.

Data boundary
-------------
A cloud agent sees only what its environment clones from the git remote. It
cannot read local files, local databases, mapped drives, or anything else on
this machine -- including production data. Briefs that depend on local data
must stay local jobs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lazytools.code_bridge import _concurrency, _store

WHERE_LOCAL = "local"
WHERE_CLOUD = "cloud"
DEFAULT_CLOUD_CONCURRENCY = 4
#: Seconds any single ``codex cloud`` call may take. Submitting and polling
#: are short network round-trips; ``apply`` downloads and applies one diff.
CLI_TIMEOUT = 300.0
#: Non-terminal statuses a cloud job record can hold. "submitting" holds a
#: place in the cloud limit while ``codex cloud exec`` is still running.
ACTIVE_STATUSES = ("submitting", "running")
TERMINAL_STATUSES = ("done", "partial", "failed", "interrupted")
#: Same rule as local jobs (``_jobs.run_job``): a job id becomes a file and
#: directory name, so only a plain token is accepted, never a path.
JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: ``wait`` gives up (exit 1) after this many polls in a row that failed, so a
#: provider that keeps erroring cannot make it hang forever.
MAX_CONSECUTIVE_POLL_ERRORS = 5
_ENV_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(sk|rk|pk|ghp|gho|ghs|ghu|github_pat|xox[abpr])[-_][A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(token|api[_-]?key|secret|password|authorization)(\s*[:=]\s*)(\S+)"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),  # JWT
)


def redact(text: str) -> str:
    """Mask credential-looking substrings before provider output is stored or printed."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text

CREATE_ENV_HINT = (
    "create a single-repo Codex Cloud environment for {repo} (https://chatgpt.com/codex/settings/environments), "
    'then map it in {path}:\n\n    [cloud.codex.envs]\n    "{repo}" = "<env id>"'
)


class CloudError(RuntimeError):
    """A cloud operation failed; the message is meant for the operator as-is."""


class CloudNotImplemented(CloudError):
    """The provider is a documented extension point, not an implementation."""


class CloudBusy(CloudError):
    def __init__(self, limit: int, holders: list[dict[str, Any]]) -> None:
        self.limit = limit
        self.holders = holders
        jobs = ", ".join(str(h.get("job_id", "?"))[:8] for h in holders) or "unknown"
        super().__init__(
            f"cloud jobs are at their concurrency limit ({limit}): {jobs} still active. "
            f"Wait for one (`lazytools-code-bridge wait <job>`), or change [cloud] concurrency in "
            f"{_concurrency.config_path()}."
        )


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CloudConfig:
    concurrency: int | None = DEFAULT_CLOUD_CONCURRENCY
    #: provider -> {"owner/name": env id}
    envs: dict[str, dict[str, str]] = field(default_factory=dict)
    path: Path | None = None


def load_config(path: Path | None = None) -> CloudConfig:
    """The ``[cloud]`` table of the bridge config; defaults when absent."""
    path = path or _concurrency.config_path()
    if not path.is_file():
        return CloudConfig(path=path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: not valid TOML: {exc}") from exc
    _concurrency.validate_top_level(data, path)
    table = data.get("cloud", {})
    if not isinstance(table, dict):
        raise ValueError(f"{path}: [cloud] must be a table")
    concurrency: int | None = DEFAULT_CLOUD_CONCURRENCY
    envs: dict[str, dict[str, str]] = {}
    for key, value in table.items():
        if key == "concurrency":
            if type(value) is not int or value < 0:
                raise ValueError(f"{path}: [cloud] concurrency must be an integer >= 0 (0 = unlimited), got {value!r}")
            concurrency = value or None
        elif key in PROVIDERS:
            if not isinstance(value, dict):
                raise ValueError(f"{path}: [cloud.{key}] must be a table")
            unknown = value.keys() - {"envs"}
            if unknown:
                raise ValueError(f"{path}: [cloud.{key}] has unknown keys: {sorted(unknown)}")
            mapping = value.get("envs", {})
            if not isinstance(mapping, dict):
                raise ValueError(f"{path}: [cloud.{key}.envs] must be a table of \"owner/name\" = \"env id\"")
            for repo, env_id in mapping.items():
                if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
                    raise ValueError(f"{path}: [cloud.{key}.envs] key {repo!r} is not 'owner/name'")
                if not isinstance(env_id, str) or not env_id.strip():
                    raise ValueError(f"{path}: [cloud.{key}.envs] {repo!r} must map to a non-empty env id string")
            envs[key] = {repo.lower(): env_id.strip() for repo, env_id in mapping.items()}
        else:
            raise ValueError(
                f"{path}: [cloud] names unknown key {key!r} (one of concurrency, {', '.join(sorted(PROVIDERS))})"
            )
    return CloudConfig(concurrency=concurrency, envs=envs, path=path)


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
    )
    if check and proc.returncode != 0:
        raise CloudError(f"git {' '.join(args)} failed in {cwd}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def repo_root(cwd: Path) -> Path:
    return Path(_git(cwd, "rev-parse", "--show-toplevel"))


def parse_remote(url: str) -> str | None:
    """``owner/name`` from an https, ssh or scp-style git remote URL."""
    url = url.strip()
    match = re.search(r"[:/]([^/:\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def repo_slug(cwd: Path, remote: str = "origin") -> str:
    url = _git(cwd, "remote", "get-url", remote, check=False)
    if not url:
        raise CloudError(f"{cwd} has no '{remote}' git remote: a cloud agent can only work on a pushed repository")
    slug = parse_remote(url)
    if slug is None:
        raise CloudError(f"cannot read 'owner/name' from the '{remote}' remote URL {url!r}")
    return slug


def current_branch(cwd: Path) -> str:
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if not branch or branch == "HEAD":
        raise CloudError(f"{cwd} is on a detached HEAD: pass --branch to say which remote branch the cloud agent starts from")
    return branch


# --------------------------------------------------------------------------- #
# status and diff parsing
# --------------------------------------------------------------------------- #

#: Provider wording -> bridge status. Checked in order; first hit wins, so
#: failure words come before the ones that also appear in progress text.
_STATUS_WORDS: tuple[tuple[str, str], ...] = (
    ("cancel", "interrupted"),
    ("error", "failed"),
    ("fail", "failed"),
    ("ready", "done"),
    ("complete", "done"),
    ("succeed", "done"),
    ("success", "done"),
    ("applied", "done"),
    ("pending", "running"),
    ("queued", "running"),
    ("running", "running"),
    ("in progress", "running"),
    ("in_progress", "running"),
    ("working", "running"),
)


def _map_status(word: str) -> str | None:
    word = word.strip().lower()
    for needle, status in _STATUS_WORDS:
        if needle in word:
            return status
    return None


@dataclass(frozen=True)
class CloudStatus:
    status: str  # one of "running", "done", "failed", "interrupted"
    raw_status: str
    message: str
    raw: str


def parse_status(text: str) -> CloudStatus:
    """Read a ``codex cloud status`` output into a bridge status.

    The CLI's output format is not a documented contract, so this is
    deliberately tolerant: a JSON object with a ``status`` field; else a
    ``status: <word>`` line (any case, optional brackets); else a known
    status word on the first line only. Everything that is not the status
    line is kept as the message. Unrecognised output counts as still running
    -- a poll must never declare a job finished on text it does not
    understand -- and the raw text is kept for the operator.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            raw_status = str(payload.get("status") or payload.get("state") or "")
            message = str(
                payload.get("message") or payload.get("final_message") or payload.get("summary") or ""
            )
            return CloudStatus(_map_status(raw_status) or "running", raw_status, message, text)
    lines = stripped.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"\s*\[?\s*(?:task\s+)?(?:status|state)\s*[:=]\s*\[?([^\]\n]+)", line, flags=re.IGNORECASE)
        if match:
            raw_status = match.group(1).strip()
            message = "\n".join(lines[:index] + lines[index + 1 :]).strip()
            return CloudStatus(_map_status(raw_status) or "running", raw_status, message, text)
    # No explicit status field: only the FIRST line may carry the status word.
    # Scanning every line would let a message such as "fixed the error path"
    # mark a running task failed.
    # And only its leading token -- "[READY] Fix error handling" is ready, not
    # failed: the rest of the line is a task title, free text like any message.
    if lines:
        head = re.match(r"\s*\[([^\]]+)\]|\s*([A-Za-z_]+(?:\s+progress)?)", lines[0])
        token = (head.group(1) or head.group(2)) if head else ""
        mapped = _map_status(token) if token else None
        if mapped is not None:
            return CloudStatus(mapped, token.strip(), "\n".join(lines[1:]).strip(), text)
    return CloudStatus("running", "", stripped, text)


@dataclass(frozen=True)
class DiffSummary:
    files: list[str]
    added: int
    removed: int

    def text(self) -> str:
        if not self.files:
            return "diff: no changes"
        head = f"diff: {len(self.files)} file(s) changed, +{self.added} -{self.removed}"
        return "\n".join([head, *(f"  {name}" for name in self.files)])


def summarize_diff(diff: str) -> DiffSummary:
    files: list[str] = []
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            files.append(match.group(2) if match else line[len("diff --git ") :])
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return DiffSummary(files, added, removed)


_TASK_URL = re.compile(r"https?://\S+/tasks?/([A-Za-z0-9_\-]+)")
_TASK_ID = re.compile(r"\b(task_[A-Za-z0-9_\-]+)\b")


def parse_submit(text: str) -> tuple[str, str | None]:
    """``(task id, task url or None)`` from ``codex cloud exec`` output."""
    url_match = _TASK_URL.search(text)
    if url_match:
        url = url_match.group(0).rstrip(".,;)")
        return url_match.group(1).rstrip(".,;)"), url
    id_match = _TASK_ID.search(text)
    if id_match:
        return id_match.group(1), None
    raise CloudError(f"codex cloud exec did not print a task URL or id; output was:\n{text.strip()[:2000]}")


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Submission:
    handle: str  # what status/diff/apply take
    url: str | None
    env_id: str
    raw: str


class CloudProvider(Protocol):
    """What the bridge needs from one cloud engine.

    ``handle`` is whatever :meth:`submit` returned and the job record stores
    as ``cloud_handle``; every later call takes it back verbatim.
    """

    name: str

    def env_for(self, repo: str, config: CloudConfig) -> str: ...

    def submit(self, *, repo_dir: Path, env_id: str, branch: str, prompt: str) -> Submission: ...

    def status(self, handle: str) -> CloudStatus: ...

    def diff(self, handle: str) -> str: ...

    def apply(self, handle: str, *, worktree: Path) -> str: ...


def _codex_argv() -> list[str]:
    """The command prefix that runs the Codex CLI with NO shell in between.

    On Windows a ``.cmd``/``.bat`` shim (what ``npm install -g`` creates) is
    run through ``cmd.exe``, which re-parses every argument -- a brief or a
    branch name containing ``&`` or ``|`` would become shell syntax. So a shim
    is never executed: the npm package's own ``bin/codex.js`` beside it is run
    with ``node`` directly, and a shim without one is refused.
    """
    from lazytools.connectors.code_support._codex import resolve_codex_bin

    found = resolve_codex_bin()
    if not found:
        raise CloudError("Codex CLI not found: install it, or set CODEX_BIN to its path")
    if Path(found).suffix.lower() not in (".cmd", ".bat"):
        return [found]
    shim_dir = Path(found).parent
    script = shim_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node = shim_dir / "node.exe"
    node_path = str(node) if node.is_file() else shutil.which("node")
    if script.is_file() and node_path:
        return [node_path, str(script)]
    raise CloudError(
        f"Codex CLI resolves to the batch shim {found}, which would pass the brief through cmd.exe; "
        "set CODEX_BIN to codex.exe (or the package's bin/codex.js is missing next to the shim)"
    )


class CodexCloud:
    """Codex Cloud through the official CLI (``codex cloud exec|status|diff|apply``)."""

    name = "codex"

    def env_for(self, repo: str, config: CloudConfig) -> str:
        env_id = config.envs.get(self.name, {}).get(repo.lower())
        if not env_id:
            path = config.path or _concurrency.config_path()
            raise CloudError(f"no Codex Cloud environment mapped for {repo}: " + CREATE_ENV_HINT.format(repo=repo, path=path))
        return env_id

    def _run(self, *args: str, cwd: Path | None = None, allow_failure: bool = False) -> tuple[int, str, str]:
        argv = [*_codex_argv(), "cloud", *args]
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CLI_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise CloudError(f"codex cloud {args[0]} timed out after {CLI_TIMEOUT:g}s") from exc
        except OSError as exc:
            raise CloudError(f"could not start the Codex CLI: {exc}") from exc
        if proc.returncode != 0 and not allow_failure:
            detail = redact((proc.stderr or proc.stdout or "").strip())
            raise CloudError(f"codex cloud {args[0]} exited {proc.returncode}: {detail[:2000]}")
        return proc.returncode, proc.stdout, proc.stderr

    def submit(self, *, repo_dir: Path, env_id: str, branch: str, prompt: str) -> Submission:
        _, out, _ = self._run("exec", "--env", env_id, "--branch", branch, prompt, cwd=repo_dir)
        handle, url = parse_submit(out)
        return Submission(handle=url or handle, url=url, env_id=env_id, raw=redact(out))

    def status(self, handle: str) -> CloudStatus:
        """Poll once. A non-zero exit is NOT automatically a poll error.

        ``codex cloud status`` reports a task that ended in error with a
        non-zero exit, and still prints its status. So the output is parsed
        whatever the exit code: a recognised status wins. A non-zero exit
        whose output names no status is a real poll failure (CLI, network,
        auth) and raises :class:`CloudError`.
        """
        code, out, err = self._run("status", handle, allow_failure=True)
        for text in (out, err):
            parsed = parse_status(redact(text))
            if parsed.raw_status:
                return parsed
        if code != 0:
            detail = redact((err or out or "").strip())
            raise CloudError(f"codex cloud status exited {code}: {detail[:2000]}")
        return parse_status(redact(out))

    def diff(self, handle: str) -> str:
        return self._run("diff", handle)[1]

    def apply(self, handle: str, *, worktree: Path) -> str:
        return redact(self._run("apply", handle, cwd=worktree)[1])


class ClaudeRoutines:
    """Extension point: Claude cloud routines (claude.ai routines API). Not implemented.

    An implementation would create a one-shot routine on a claude.ai cloud
    environment for the repository, with the brief as its prompt, store the
    returned session id as the handle, poll the session for its terminal
    state, read the branch it pushed as the "diff", and fetch that branch into
    the apply worktree. Register it in :data:`PROVIDERS` by replacing this class.
    """

    name = "claude"

    def _refuse(self) -> CloudNotImplemented:
        return CloudNotImplemented(
            "cloud jobs for --engine claude (claude.ai routines) are not implemented yet; "
            "use --where cloud --engine codex, or run Claude locally. See docs/code-bridge.md, 'Cloud jobs'."
        )

    def env_for(self, repo: str, config: CloudConfig) -> str:
        raise self._refuse()

    def submit(self, *, repo_dir: Path, env_id: str, branch: str, prompt: str) -> Submission:
        raise self._refuse()

    def status(self, handle: str) -> CloudStatus:
        raise self._refuse()

    def diff(self, handle: str) -> str:
        raise self._refuse()

    def apply(self, handle: str, *, worktree: Path) -> str:
        raise self._refuse()


PROVIDERS: dict[str, type] = {"codex": CodexCloud, "claude": ClaudeRoutines}


def provider(engine: str) -> CloudProvider:
    try:
        return PROVIDERS[engine]()
    except KeyError:
        raise CloudError(f"no cloud provider for engine {engine!r} (one of {', '.join(PROVIDERS)})") from None


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

#: Prepended to every cloud brief: the agent must not assume it can reach
#: anything the local bridge could.
CLOUD_PREAMBLE = (
    "You are running as a cloud coding agent dispatched by lazytools-code-bridge. "
    "You only have the repository as cloned from its git remote: no local files, databases or "
    "production data of the operator's machine are reachable. Do not push to the default branch; "
    "leave your changes as this task's diff.\n\n"
)


def is_cloud(job: dict[str, Any] | None) -> bool:
    return job is not None and job.get("where") == WHERE_CLOUD


def _write(store: Any, registry: Any, job_id: str, meta: dict[str, Any], *, status: str,
           result: str | None = None, error: str | None = None) -> None:
    meta = {**meta, "updated_at": time.time()}
    _store.write_meta(store, job_id, meta)
    registry.write(
        job_id,
        meta.get("objective", ""),
        tool_name=meta["engine"],
        status=status,
        result=result,
        error=error,
        engine=meta["engine"],
        extra={"where": WHERE_CLOUD, "cloud_handle": meta.get("cloud_handle")},
    )


def _write_unless_terminal(store: Any, registry: Any, job_id: str, updates: dict[str, Any], *, status: str,
                           result: str | None = None, error: str | None = None) -> bool:
    """Persist a status change only if the stored record is not already terminal.

    Polls and submits talk to the provider OUTSIDE any lock, so by the time
    their answer arrives another process may have cancelled the job (or a
    concurrent poll may have finished it). Re-reading under the machine-wide
    guard and refusing to overwrite a terminal record keeps a stale answer
    from resurrecting a cancelled job (and its concurrency place). The
    ``updates`` (handle, poll metadata) are merged into the latest meta, so
    nothing another writer stored is lost. Returns whether the status was written.
    """
    with _concurrency._guard(_concurrency.load_config().slot_dir):
        current = registry.find(job_id) or {}
        meta = {**(_store.read_meta(store, job_id) or {}), **updates}
        if current.get("status") in TERMINAL_STATUSES:
            # Keep the terminal status/result/error; only the metadata moves.
            _write(store, registry, job_id, meta, status=str(current["status"]),
                   result=current.get("result"), error=current.get("error"))
            return False
        _write(store, registry, job_id, meta, status=status, result=result, error=error)
        return True


def mark_interrupted(store: Any, registry: Any, job_id: str, error: str) -> bool:
    """Record a cloud job interrupted (operator cancel), unless it already ended."""
    return _write_unless_terminal(store, registry, job_id, {}, status="interrupted", error=error)


def active_jobs(store: Any) -> list[dict[str, Any]]:
    rows = []
    for job in _store.all_job_records(store):
        if job.get("where") == WHERE_CLOUD and job.get("status") in ACTIVE_STATUSES:
            meta = _store.read_meta(store, str(job.get("job_id", ""))) or {}
            rows.append({**meta, **job})
    return rows


def refresh(store: Any, registry: Any, job: dict[str, Any]) -> dict[str, Any]:
    """Poll the provider once and persist the job's new status; return the merged record.

    A terminal record is returned unchanged. A record still "submitting"
    (no handle yet) is returned unchanged too. A poll failure is NOT a job
    failure: it is recorded as ``last_poll_error`` and the job stays running.
    """
    if job.get("status") in TERMINAL_STATUSES or not job.get("cloud_handle"):
        return job
    job_id = str(job["job_id"])
    try:
        state = provider(str(job.get("engine"))).status(str(job["cloud_handle"]))
    except CloudError as exc:
        updates = {
            "last_poll_error": redact(str(exc)),
            "last_polled_at": time.time(),
            "poll_errors_in_a_row": int(job.get("poll_errors_in_a_row") or 0) + 1,
        }
        _write_unless_terminal(store, registry, job_id, updates, status=str(job.get("status") or "running"))
        return _merged(store, registry, job_id)
    updates = {
        "cloud_status": state.raw_status,
        "cloud_message": state.message,
        "last_polled_at": time.time(),
        "last_poll_error": None,
        "poll_errors_in_a_row": 0,
    }
    if state.status == "done":
        _write_unless_terminal(store, registry, job_id, updates, status="done",
                               result=state.message or state.raw.strip())
    elif state.status in ("failed", "interrupted"):
        _write_unless_terminal(store, registry, job_id, updates, status=state.status,
                               error=f"cloud task {state.raw_status or state.status}: {state.message or state.raw.strip()}")
    else:
        _write_unless_terminal(store, registry, job_id, updates, status="running")
    return _merged(store, registry, job_id)


def _merged(store: Any, registry: Any, job_id: str) -> dict[str, Any]:
    record = registry.find(job_id) or {}
    return {**(_store.read_meta(store, job_id) or {}), **record}


def _admit(store: Any, registry: Any, config: CloudConfig, job_id: str, meta: dict[str, Any]) -> None:
    """Take a cloud place for ``job_id`` (record it "submitting"), or raise :class:`CloudBusy`."""
    limit = config.concurrency
    if limit:
        # Refresh outside the guard (network calls), so tasks that finished
        # since their last poll free their place before we count.
        active = active_jobs(store)
        if len(active) >= limit:
            for job in active:
                refresh(store, registry, job)
    slot_dir = _concurrency.load_config().slot_dir
    with _concurrency._guard(slot_dir):
        active = active_jobs(store)
        if limit and len(active) >= limit:
            raise CloudBusy(limit, active)
        _write(store, registry, job_id, meta, status="submitting")


def _remote_base(root: Path, branch: str) -> str | None:
    """The commit the cloud agent will start from: the remote's tip of ``branch``.

    Fetched first (best effort) so the recorded commit is the one the cloud
    environment clones, not a stale remote-tracking ref. ``None`` when the
    branch is not on the remote (or the fetch and the ref both fail).
    """
    _git(root, "fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)
    sha = _git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{commit}}", check=False)
    return sha or None


def submit_job(
    *,
    engine: str,
    cwd: Path,
    task: str,
    branch: str | None = None,
    db_path: Path | None = None,
    job_id: str | None = None,
    config: CloudConfig | None = None,
) -> dict[str, Any]:
    """Submit ``task`` to the cloud provider for ``engine``; return the job record.

    Raises :class:`CloudError` (no job recorded) for a missing env mapping,
    a repository without a usable remote, or the cloud limit; a failure of
    the submit call itself is recorded as a failed job and re-raised.
    """
    import uuid

    if job_id is not None and not JOB_ID_RE.fullmatch(job_id):
        raise CloudError(f"job id must be 1-64 letters, digits, '-' or '_', got {job_id!r}")
    config = config or load_config()
    prov = provider(engine)
    root = repo_root(cwd)
    repo = repo_slug(root)
    env_id = prov.env_for(repo, config)
    if not _ENV_ID_RE.fullmatch(env_id):
        raise CloudError(f"env id {env_id!r} for {repo} has unexpected characters")
    branch = branch or current_branch(root)
    if branch.startswith("-") or _git(root, "check-ref-format", "--branch", branch, check=False) != branch:
        raise CloudError(f"{branch!r} is not a valid branch name")
    job_id = job_id or uuid.uuid4().hex
    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    if _store.read_meta(store, job_id) is not None or registry.find(job_id) is not None:
        raise CloudError(f"job id {job_id!r} already exists -- refusing to overwrite its record")
    base_commit = _remote_base(root, branch)
    meta: dict[str, Any] = {
        "job_id": job_id,
        "engine": engine,
        "where": WHERE_CLOUD,
        "cwd": str(cwd),
        "repo_root": str(root),
        "repo": repo,
        "env_id": env_id,
        "branch": branch,
        "base_commit": base_commit,
        "objective": task,
        "created_at": time.time(),
        "cloud_handle": None,
    }
    _admit(store, registry, config, job_id, meta)
    try:
        sub = prov.submit(repo_dir=root, env_id=env_id, branch=branch, prompt=CLOUD_PREAMBLE + task)
    except BaseException as exc:
        _write(store, registry, job_id, meta, status="failed", error=f"cloud submit failed: {exc}")
        raise
    # Cancelled while `codex cloud exec` ran: keep "interrupted", but still
    # record the handle so the operator can find and stop the remote task.
    _write_unless_terminal(
        store, registry, job_id, {"cloud_handle": sub.handle, "cloud_url": sub.url, "submitted_at": time.time()},
        status="running",
    )
    return _merged(store, registry, job_id)


def result_text(job: dict[str, Any]) -> str:
    """Final message plus a summary of the task's diff (fetched live)."""
    parts = [f"[{job.get('status')}] cloud job {job.get('job_id')} ({job.get('engine')}, {job.get('cloud_url') or job.get('cloud_handle')})"]
    message = job.get("result") or job.get("cloud_message") or job.get("error") or ""
    if message:
        parts.append(str(message).strip())
    if job.get("cloud_handle"):
        try:
            parts.append(summarize_diff(provider(str(job["engine"])).diff(str(job["cloud_handle"]))).text())
        except CloudError as exc:
            parts.append(f"diff: unavailable ({exc})")
    return "\n".join(parts)


def worktrees_dir(db_path: Path | None = None) -> Path:
    return (db_path or _store.default_db_path()).parent / "cloud-worktrees"


def apply_job(
    store: Any,
    registry: Any,
    job: dict[str, Any],
    *,
    db_path: Path | None = None,
    branch: str | None = None,
    worktree: Path | None = None,
) -> dict[str, Any]:
    """Apply a finished cloud task in a NEW git worktree on a NEW branch.

    Never touches the operator's checkout: ``git worktree add -b`` creates
    both, starting from the job's base branch (``origin/<branch>`` when the
    remote-tracking ref exists, else the local branch). Nothing is pushed --
    pushing stays a separate, approved step.
    """
    if not job.get("cloud_handle"):
        raise CloudError(f"job {job.get('job_id')} has no cloud task to apply")
    if job.get("status") != "done":
        raise CloudError(f"job {job.get('job_id')} is {job.get('status')}, not done: only a finished task can be applied")
    if job.get("applied_worktree"):
        raise CloudError(f"job {job['job_id']} was already applied in {job['applied_worktree']}")
    job_id = str(job["job_id"])
    if not JOB_ID_RE.fullmatch(job_id):
        raise CloudError(f"job id {job_id!r} is not a plain token; refusing to derive paths from it")
    root = Path(str(job.get("repo_root") or repo_root(Path(str(job["cwd"])))))
    branch = branch or f"cloud/{job_id[:8]}"
    if branch.startswith("-") or _git(root, "check-ref-format", "--branch", branch, check=False) != branch:
        raise CloudError(f"{branch!r} is not a valid branch name")
    if worktree is None:
        container = worktrees_dir(db_path).expanduser().resolve()
        worktree = (container / job_id).resolve()
        if worktree.parent != container:
            raise CloudError(f"{worktree} is outside {container}")
    else:
        worktree = worktree.expanduser().resolve()
    if worktree.exists():
        raise CloudError(f"{worktree} already exists: remove it or pass --worktree")
    base_branch = str(job.get("branch") or "HEAD")
    base_commit = job.get("base_commit")
    if base_commit and _git(root, "rev-parse", "--verify", "--quiet", f"{base_commit}^{{commit}}", check=False):
        # The exact commit the cloud task started from: the diff applies onto
        # what the agent saw, even if the branch has moved since.
        base = str(base_commit)
    else:
        base = f"origin/{base_branch}"
        if not _git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/{base}", check=False):
            base = base_branch
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", branch, str(worktree), base)
    try:
        output = provider(str(job["engine"])).apply(str(job["cloud_handle"]), worktree=worktree)
    except BaseException as exc:
        _after_failed_apply(store, registry, root, job_id, worktree, branch, exc)
        raise
    status_lines = _git(worktree, "status", "--porcelain", check=False)
    meta = _store.read_meta(store, job_id) or {}
    meta.update(applied_worktree=str(worktree), applied_branch=branch, applied_base=base, applied_at=time.time())
    moved = None
    if base_commit and base == base_commit:
        tip = _git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_branch}", check=False)
        moved = tip if tip and tip != base_commit else None
    _write(store, registry, job_id, meta, status=str(job["status"]), result=job.get("result"), error=job.get("error"))
    return {
        "job_id": job_id,
        "worktree": str(worktree),
        "branch": branch,
        "base": base,
        "changed": [line for line in status_lines.splitlines() if line.strip()],
        "output": output.strip(),
        "pushed": False,
        "branch_moved_to": moved,
        "base_branch": base_branch,
    }


def _after_failed_apply(store: Any, registry: Any, root: Path, job_id: str, worktree: Path, branch: str,
                        exc: BaseException) -> None:
    """Make a failed apply retryable without losing anything it changed.

    Untouched worktree (no changes at all): remove it and its branch, so the
    same ``apply <job>`` can simply be run again. Partial changes: keep both,
    record where they are, and say so -- they are never deleted blindly.
    """
    dirty = _git(worktree, "status", "--porcelain", check=False) if worktree.exists() else ""
    if not dirty:
        _git(root, "worktree", "remove", "--force", str(worktree), check=False)
        _git(root, "branch", "-D", branch, check=False)
        return
    meta = _store.read_meta(store, job_id) or {}
    meta.update(apply_failed_worktree=str(worktree), apply_failed_branch=branch, apply_error=str(exc))
    _store.write_meta(store, job_id, meta)
    if isinstance(exc, CloudError):
        exc.args = (
            f"{exc} -- partial changes kept in {worktree} (branch {branch}); inspect them, then remove that "
            f"worktree and branch (git worktree remove {worktree}; git branch -D {branch}) before retrying, "
            "or retry with --worktree and --branch",
        )


def wait_job(store_factory: Any, job_id: str, *, timeout: float | None, interval: float) -> dict[str, Any] | None:
    """Poll a cloud job until terminal or ``timeout``; return the last merged record (None if gone)."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        store = store_factory()
        registry = _store.build_job_registry(store)
        job = registry.find(job_id)
        if job is None:
            return None
        job = refresh(store, registry, _merged(store, registry, str(job["job_id"])))
        if job.get("status") in TERMINAL_STATUSES:
            return job
        if int(job.get("poll_errors_in_a_row") or 0) >= MAX_CONSECUTIVE_POLL_ERRORS:
            return job  # the caller reports it; never wait forever on a provider that keeps failing
        if deadline is not None and time.monotonic() >= deadline:
            return job
        time.sleep(interval)


__all__ = [
    "ACTIVE_STATUSES",
    "JOB_ID_RE",
    "MAX_CONSECUTIVE_POLL_ERRORS",
    "CLOUD_PREAMBLE",
    "DEFAULT_CLOUD_CONCURRENCY",
    "PROVIDERS",
    "WHERE_CLOUD",
    "WHERE_LOCAL",
    "ClaudeRoutines",
    "CloudBusy",
    "CloudConfig",
    "CloudError",
    "CloudNotImplemented",
    "CloudProvider",
    "CloudStatus",
    "CodexCloud",
    "active_jobs",
    "apply_job",
    "is_cloud",
    "load_config",
    "parse_remote",
    "parse_status",
    "parse_submit",
    "provider",
    "redact",
    "refresh",
    "repo_slug",
    "result_text",
    "submit_job",
    "summarize_diff",
    "wait_job",
]
