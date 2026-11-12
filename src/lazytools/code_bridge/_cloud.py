"""Cloud dispatch: hand a brief to a CLOUD coding agent instead of a local engine.

A local job runs Codex or Claude Code on the owner's machine, where it
competes with production jobs during production windows. A cloud job runs on
the provider's infrastructure: this module only submits the brief, records the
returned task handle, and later polls / fetches / applies the result. Nothing
heavy runs locally, and no local process stays alive for the job.

Providers
---------
Each cloud engine is a :class:`CloudProvider`. Codex Cloud
(:class:`CodexCloud`) drives the official ``codex cloud`` CLI. Claude Code
cloud sessions (``_cloud_claude.ClaudeCloud``) use ``claude --cloud`` or a
routine's fire endpoint; Claude has no status API, so its status and result
come from the ``claude/...`` branch the session pushes. See
``docs/code-bridge.md`` ("Cloud jobs") and ``docs/code-bridge-cloud-setup.md``.

Configuration (``~/.lazytools/code-bridge.toml``, same file as ``[concurrency]``)::

    [cloud]
    concurrency = 4          # live cloud jobs at once, all providers; 0 = unlimited
    default_where = "local"  # what `run` does without --where: local | cloud | auto
    busy_windows = []        # e.g. ["21:00-01:30"]: local time ranges where `auto` sends jobs to the cloud

    [cloud.codex]
    concurrency = 2          # optional per-provider limit, in addition to the global one

    [cloud.codex.envs]
    "selvaz/LazyTools" = "env_0123abcd"   # git remote 'owner/name' -> Codex Cloud env id

    [cloud.claude]
    concurrency = 2

    [cloud.claude.models]    # tier -> model for Claude cloud sessions (default: claude-sonnet-5-5)
    critical = "claude-opus-5-5"

    [cloud.claude.routines.nightly]       # optional: fire a claude.ai routine instead of `claude --cloud`
    id = "trig_0123"
    token_env = "LAZYTOOLS_ROUTINE_NIGHTLY_TOKEN"   # the env var NAME; tokens never go in this file

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

from lazytools.code_bridge import _cloud_place, _concurrency, _store

WHERE_LOCAL = "local"
WHERE_CLOUD = "cloud"
DEFAULT_CLOUD_CONCURRENCY = 4
#: Model a Claude cloud session runs when neither ``--model``, a mapped tier nor
#: ``[cloud.claude.models] default`` names one. Codex Cloud has no such setting:
#: the CLI ignores ``-c model`` for cloud tasks, so the environment's default applies.
DEFAULT_CLAUDE_CLOUD_MODEL = "claude-sonnet-5-5"
CLOUD_TIERS = ("basic", "writing", "thinking", "critical")
SETUP_DOC = "docs/code-bridge-cloud-setup.md"
_MODEL_RE = re.compile(r"[A-Za-z0-9_.:/\[\]-]{1,100}")
#: Seconds any single ``codex cloud`` call may take. Submitting and polling
#: are short network round-trips; ``apply`` downloads and applies one diff.
CLI_TIMEOUT = 300.0
#: Non-terminal statuses a cloud job record can hold. "submitting" holds a
#: place in the cloud limit while ``codex cloud exec`` is still running.
ACTIVE_STATUSES = ("submitting", "running")
TERMINAL_STATUSES = ("done", "partial", "failed", "interrupted")
#: A cloud job id becomes a file and directory name (``cloud-worktrees/<id>``),
#: so only lowercase hex (8-32 digits, what ``uuid4().hex`` produces) or a
#: canonical UUID is accepted -- never anything that could be a path.
JOB_ID_RE = re.compile(r"[0-9a-f]{8,32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: After this many polls in a row that failed or returned output naming no
#: status, the job is marked ``failed`` (a terminal status, so ``wait`` stops
#: and the cloud place is freed) with a redacted reason.
MAX_CONSECUTIVE_POLL_ERRORS = 5
#: ``wait`` on a cloud job never runs unbounded: without ``--timeout`` it
#: gives up after this many seconds (exit 3).
DEFAULT_CLOUD_WAIT_TIMEOUT = 2 * 3600.0
#: Actions a local job's BridgeGate would hold for a human ticket, as
#: ``--approve-cloud`` names. A cloud agent's commands cannot be gated one by
#: one, so a brief that asks for one is refused unless the operator approves
#: that action up front. Commands in the brief are classified by the SAME rule
#: table the local gate uses (``_policy.CODING_RULES``, via
#: ``_policy.cloud_rule_actions``); :data:`_PROSE_INTENTS` only adds requests
#: written in words ("push the branch", "open a PR") that no command pattern can see.
GATED_ACTIONS: tuple[str, ...] = ("push", "pr-create", "merge", "delete", "reset", "commit", "git-option")
#: Natural-language requests for a gated action, plus commands the local gate
#: asks for only through its catch-all (``git merge``, ``git branch -D``,
#: ``git clean``), which have no named rule of their own.
_PROSE_INTENTS: dict[str, re.Pattern[str]] = {
    "push": re.compile(r"(?i)\b(?:force[- ]?)?push(?:es|ed|ing)?\b"),
    "pr-create": re.compile(
        r"(?i)\b(?:open|create|file|raise|submit|send)(?:s|ed|ing)?\s+(?:a\s+|an\s+|the\s+|new\s+)*"
        r"(?:draft\s+)?(?:pr|pull[- ]request)s?\b"
    ),
    "merge": re.compile(
        r"(?i)\bgit\s+merge\b|\bmerge\s+(?:it|this|that)\b"
        r"|\bmerg(?:e|es|ed|ing)\b[^.\n]{0,40}\b(?:pr|pull\s+request|branch|into|main|master|trunk)\b"
    ),
    "delete": re.compile(
        r"(?i)\bgit\s+branch\s+-[dD]\b|\bgit\s+clean\b"
        r"|\b(?:delet|remov)(?:e|es|ed|ing)\b[^.\n]{0,40}\b(?:branch(?:es)?|tags?|repo(?:sitory)?|releases?|remote)\b"
    ),
}
_ENV_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_ROUTINE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ROUTINE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_ENV_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
#: Keys allowed under ``[cloud.<provider>]``.
_PROVIDER_KEYS: dict[str, frozenset[str]] = {
    "codex": frozenset({"envs", "concurrency"}),
    "claude": frozenset({"routines", "concurrency", "models"}),
}

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(sk|rk|pk|ghp|gho|ghs|ghu|github_pat|xox[abpr])[-_][A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(token|api[_-]?key|secret|password|authorization)(\s*[:=]\s*)(\S+)"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),  # JWT
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access key id
    re.compile(r"\b(?:glpat|npm|AIza)[-_A-Za-z0-9]{16,}"),  # GitLab / npm / Google API keys
)


def redact(text: str) -> str:
    """Mask credential-looking substrings before provider output is stored or printed."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text


def redact_obj(value: Any) -> Any:
    """:func:`redact` every string inside ``value`` (dicts, lists, tuples), keys included.

    For JSON output: redacting the serialised text instead could cut through
    a closing quote and leave invalid JSON.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact_obj(key): redact_obj(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(item) for item in value]
    return value


def gated_actions(text: str) -> list[str]:
    """The :data:`GATED_ACTIONS` a brief asks for, in a stable order.

    Every command the local gate's rule table would ask for (or session-ask
    for on first use) counts, plus the natural-language requests in
    :data:`_PROSE_INTENTS`.
    """
    from lazytools.code_bridge._policy import cloud_rule_actions

    found = cloud_rule_actions(text)
    found.update(name for name, pattern in _PROSE_INTENTS.items() if pattern.search(text))
    return [name for name in GATED_ACTIONS if name in found]


def _inside(path: Path, container: Path) -> bool:
    """``path`` resolves to ``container`` itself or somewhere below it."""
    return path.resolve().is_relative_to(container.resolve())

CREATE_ENV_HINT = (
    "create a single-repo Codex Cloud environment for {repo} (https://chatgpt.com/codex/settings/environments), "
    'then map it in {path}:\n\n    [cloud.codex.envs]\n    "{repo}" = "<env id>"\n\nSee docs/code-bridge-cloud-setup.md '
    "for the full walk-through (`lazytools-code-bridge cloud-envs` lists what is configured)."
)


class CloudError(RuntimeError):
    """A cloud operation failed; the message is meant for the operator as-is.

    Every message is passed through :func:`redact` when the error is built,
    so provider output quoted in one (a submit that printed no task id, a
    failed ``codex cloud`` call) never carries a credential to a terminal,
    a log or the job record.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*(redact(arg) if isinstance(arg, str) else arg for arg in args))


class CloudNotImplemented(CloudError):
    """The provider is a documented extension point, not an implementation."""


class CloudBusy(CloudError):
    def __init__(self, limit: int, holders: list[dict[str, Any]], provider: str | None = None) -> None:
        self.limit = limit
        self.holders = holders
        self.provider = provider
        jobs = ", ".join(str(h.get("job_id", "?"))[:8] for h in holders) or "unknown"
        scope = f"{provider} cloud jobs" if provider else "cloud jobs"
        knob = f"[cloud.{provider}] concurrency" if provider else "[cloud] concurrency"
        super().__init__(
            f"{scope} are at their concurrency limit ({limit}): {jobs} still active. "
            f"Wait for one (`lazytools-code-bridge wait <job>`), or change {knob} in "
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
    #: routine name -> {"id": routine id, "token_env": env var holding its bearer token}
    routines: dict[str, dict[str, str]] = field(default_factory=dict)
    path: Path | None = None
    #: what ``run`` does without ``--where``: "local", "cloud" or "auto"
    default_where: str = "local"
    #: local-time ranges ("21:00-01:30") in which ``auto`` prefers the cloud
    busy_windows: tuple[str, ...] = ()
    #: provider -> its own live-job limit (None = unlimited), on top of ``concurrency``
    provider_concurrency: dict[str, int | None] = field(default_factory=dict)
    #: provider -> {tier or "default": model}
    models: dict[str, dict[str, str]] = field(default_factory=dict)


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
    routines: dict[str, dict[str, str]] = {}
    default_where = "local"
    windows: tuple[str, ...] = ()
    provider_limits: dict[str, int | None] = {}
    models: dict[str, dict[str, str]] = {}
    for key, value in table.items():
        if key == "concurrency":
            if type(value) is not int or value < 0:
                raise ValueError(f"{path}: [cloud] concurrency must be an integer >= 0 (0 = unlimited), got {value!r}")
            concurrency = value or None
        elif key == "default_where":
            if value not in _cloud_place.WHERE_CHOICES:
                raise ValueError(f"{path}: [cloud] default_where must be one of "
                                 f"{', '.join(_cloud_place.WHERE_CHOICES)}, got {value!r}")
            default_where = value
        elif key == "busy_windows":
            if not isinstance(value, list) or not all(isinstance(w, str) for w in value):
                raise ValueError(f'{path}: [cloud] busy_windows must be a list of "HH:MM-HH:MM" strings')
            try:
                for window in value:
                    _cloud_place.parse_window(window)
            except ValueError as exc:
                raise ValueError(f"{path}: [cloud] {exc}") from exc
            windows = tuple(value)
        elif key in PROVIDERS:
            if not isinstance(value, dict):
                raise ValueError(f"{path}: [cloud.{key}] must be a table")
            unknown = value.keys() - _PROVIDER_KEYS[key]
            if key == "codex" and "models" in unknown:
                raise ValueError(
                    f"{path}: [cloud.codex.models] is not supported: the Codex CLI ignores `-c model` for cloud "
                    "tasks, so a Codex Cloud task always runs the model its environment is set to (change it in "
                    "the environment's settings at chatgpt.com/codex)"
                )
            if key == "claude" and "envs" in unknown:
                raise ValueError(
                    f"{path}: [cloud.claude.envs] is not supported: `claude --cloud` has no environment flag; "
                    "choose the default cloud environment at claude.ai/code (see docs/code-bridge-cloud-setup.md)"
                )
            if unknown:
                raise ValueError(f"{path}: [cloud.{key}] has unknown keys: {sorted(unknown)}")
            if "concurrency" in value:
                limit = value["concurrency"]
                if type(limit) is not int or limit < 0:
                    raise ValueError(f"{path}: [cloud.{key}] concurrency must be an integer >= 0 (0 = unlimited), "
                                     f"got {limit!r}")
                provider_limits[key] = limit or None
            if key == "claude":
                routines = _load_routines(value.get("routines", {}), path)
                models["claude"] = _load_models(value.get("models", {}), path)
                continue
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
                f"{path}: [cloud] names unknown key {key!r} (one of concurrency, default_where, busy_windows, "
                f"{', '.join(sorted(PROVIDERS))})"
            )
    return CloudConfig(concurrency=concurrency, envs=envs, routines=routines, path=path, default_where=default_where,
                       busy_windows=windows, provider_concurrency=provider_limits, models=models)


def _load_models(table: Any, path: Path) -> dict[str, str]:
    """Validate ``[cloud.claude.models]``: tier (or ``default``) -> model id."""
    if not isinstance(table, dict):
        raise ValueError(f"{path}: [cloud.claude.models] must be a table of tier = \"model id\"")
    for tier, model in table.items():
        if tier != "default" and tier not in CLOUD_TIERS:
            raise ValueError(f"{path}: [cloud.claude.models] key {tier!r} is not a tier "
                             f"({', '.join(CLOUD_TIERS)}) or 'default'")
        if not isinstance(model, str) or not _MODEL_RE.fullmatch(model.strip()):
            raise ValueError(f"{path}: [cloud.claude.models] {tier!r} must map to a model id string")
    return {tier: model.strip() for tier, model in table.items()}


def model_for(engine: str, config: CloudConfig, *, tier: str | None = None, explicit: str | None = None,
              routine: str | None = None) -> tuple[str | None, str | None]:
    """``(model to request, note)`` for a cloud job.

    Claude cloud: ``--model``, else the tier's ``[cloud.claude.models]`` entry,
    else its ``default`` entry, else :data:`DEFAULT_CLAUDE_CLOUD_MODEL`. A
    routine fixes its own model. Codex Cloud always uses its environment's
    default (the CLI ignores ``-c model`` for cloud), so an explicit model is
    refused rather than silently dropped.
    """
    if engine == "codex":
        if explicit:
            raise CloudError("Codex Cloud ignores model overrides (the CLI ignores `-c model` for cloud tasks); the "
                             "environment's own default model applies. Drop --model, or use --engine claude")
        return None, "Codex Cloud uses the environment's default model" if tier else None
    if routine is not None:
        if explicit:
            raise CloudError("--model cannot override a routine's model: change it in the routine's settings")
        return None, "the routine's own model applies"
    table = config.models.get("claude", {})
    return (explicit or (tier and table.get(tier)) or table.get("default") or DEFAULT_CLAUDE_CLOUD_MODEL), None


def _load_routines(table: Any, path: Path) -> dict[str, dict[str, str]]:
    """Validate ``[cloud.claude.routines]``: name -> {id, token_env}; a literal token is refused."""
    if not isinstance(table, dict):
        raise ValueError(f"{path}: [cloud.claude.routines] must be a table of name = {{ id, token_env }}")
    routines: dict[str, dict[str, str]] = {}
    for name, entry in table.items():
        where = f"{path}: [cloud.claude.routines.{name}]"
        if not _ROUTINE_NAME_RE.fullmatch(name):
            raise ValueError(f"{where}: routine names may use letters, digits, '_', '.', '-' only")
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a table with id and token_env")
        if "token" in entry or "bearer" in entry:
            raise ValueError(
                f"{where}: never store the token in the config; put it in an environment variable and set "
                "token_env to that variable's NAME"
            )
        unknown = entry.keys() - {"id", "token_env"}
        if unknown:
            raise ValueError(f"{where} has unknown keys: {sorted(unknown)} (only id, token_env)")
        routine_id, token_env = entry.get("id"), entry.get("token_env")
        if not isinstance(routine_id, str) or not _ROUTINE_ID_RE.fullmatch(routine_id):
            raise ValueError(f"{where}: id must be the routine id (letters, digits, '_', '-'), e.g. \"trig_...\"")
        if not isinstance(token_env, str) or not _ENV_VAR_RE.fullmatch(token_env):
            raise ValueError(f"{where}: token_env must be an environment variable NAME, e.g. LAZYTOOLS_ROUTINE_TOKEN")
        routines[name] = {"id": routine_id, "token_env": token_env}
    return routines


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str, check: bool = True, timeout: float = 60) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
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
    #: provider-specific fields merged into the job record (e.g. Claude's result branch)
    extra: dict[str, Any] = field(default_factory=dict)


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
    #: provider-specific fields stored on the job record at submit
    extra: dict[str, Any] = field(default_factory=dict)


class CloudProvider(Protocol):
    """What the bridge needs from one cloud engine.

    ``handle`` is whatever :meth:`submit` returned and the job record stores
    as ``cloud_handle``; every later call takes it back verbatim.
    """

    name: str

    def env_for(self, repo: str, config: CloudConfig, routine: str | None = None) -> str: ...

    def submit(self, *, repo_dir: Path, env_id: str, branch: str, prompt: str, **context: Any) -> Submission: ...

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

    def env_for(self, repo: str, config: CloudConfig, routine: str | None = None) -> str:
        if routine is not None:
            raise CloudError("--routine is only for --engine claude (claude.ai routines)")
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

    def submit(self, *, repo_dir: Path, env_id: str, branch: str, prompt: str, **_context: Any) -> Submission:
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


def _claude_provider() -> CloudProvider:
    from lazytools.code_bridge._cloud_claude import ClaudeCloud

    return ClaudeCloud()


PROVIDERS: dict[str, Any] = {"codex": CodexCloud, "claude": _claude_provider}


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


def active_jobs(store: Any, engine: str | None = None) -> list[dict[str, Any]]:
    """Live cloud jobs (``engine``: only that provider's)."""
    rows = []
    for job in _store.all_job_records(store):
        if job.get("where") == WHERE_CLOUD and job.get("status") in ACTIVE_STATUSES:
            meta = _store.read_meta(store, str(job.get("job_id", ""))) or {}
            merged = {**meta, **job}
            if engine is None or merged.get("engine") == engine:
                rows.append(merged)
    return rows


def refresh(store: Any, registry: Any, job: dict[str, Any]) -> dict[str, Any]:
    """Poll the provider once and persist the job's new status; return the merged record.

    A terminal record is returned unchanged. A record still "submitting"
    (no handle yet) is returned unchanged too. A failed poll -- the CLI
    erred without naming a status, or its output names no status at all --
    is recorded as ``last_poll_error`` and the job stays running, up to
    :data:`MAX_CONSECUTIVE_POLL_ERRORS` in a row; the poll that reaches that
    bound marks the job ``failed`` with the (redacted) last reason, so no
    caller waits forever on output it cannot read.
    """
    if job.get("status") in TERMINAL_STATUSES or not job.get("cloud_handle"):
        return job
    job_id = str(job["job_id"])
    poll_error: str | None = None
    try:
        prov = provider(str(job.get("engine")))
        job_status = getattr(prov, "job_status", None)
        if job_status is not None:
            # No status API (Claude): the provider reads the job's result from git.
            state = job_status(job, claimed=_claimed_result_branches(store, job_id, job.get("repo")))
        else:
            state = prov.status(str(job["cloud_handle"]))
    except CloudError as exc:
        poll_error = redact(str(exc))
    else:
        if not state.raw_status:
            poll_error = "cloud status printed no recognisable status: " + redact(state.raw.strip())[:500]
    if poll_error is not None:
        errors = int(job.get("poll_errors_in_a_row") or 0) + 1
        updates = {"last_poll_error": poll_error, "last_polled_at": time.time(), "poll_errors_in_a_row": errors}
        if errors >= MAX_CONSECUTIVE_POLL_ERRORS:
            _write_unless_terminal(store, registry, job_id, updates, status="failed",
                                   error=f"cloud status unreadable after {errors} polls in a row; last: {poll_error}")
        else:
            _write_unless_terminal(store, registry, job_id, updates, status=str(job.get("status") or "running"))
        return _merged(store, registry, job_id)
    updates = {
        "cloud_status": state.raw_status,
        "cloud_message": state.message,
        "last_polled_at": time.time(),
        "last_poll_error": None,
        "poll_errors_in_a_row": 0,
        **state.extra,
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


def _claimed_result_branches(store: Any, job_id: str, repo: str | None) -> set[str]:
    """Result branches already attributed to OTHER cloud jobs on the SAME repository.

    Two Claude jobs never share one result branch; a branch name used in one
    repository does not reserve that name in another.
    """
    claimed = set()
    repo_key = (repo or "").lower()
    for job in _store.all_job_records(store):
        other = str(job.get("job_id", ""))
        if other == job_id or job.get("where") != WHERE_CLOUD:
            continue
        meta = _store.read_meta(store, other) or {}
        if str(meta.get("repo") or "").lower() != repo_key:
            continue
        branch = meta.get("result_branch")
        if branch:
            claimed.add(str(branch))
    return claimed


def _merged(store: Any, registry: Any, job_id: str) -> dict[str, Any]:
    record = registry.find(job_id) or {}
    return {**(_store.read_meta(store, job_id) or {}), **record}


def _admit(store: Any, registry: Any, config: CloudConfig, job_id: str, meta: dict[str, Any]) -> None:
    """Take a cloud place for ``job_id`` (record it "submitting"), or raise :class:`CloudBusy`.

    Raises :class:`CloudError` if ``job_id`` already has a record; that check
    is atomic with the insert (both under the guard lock).
    """
    limit = config.concurrency
    engine = str(meta.get("engine"))
    provider_limit = config.provider_concurrency.get(engine)
    if limit or provider_limit:
        # Refresh outside the guard (network calls), so tasks that finished
        # since their last poll free their place before we count.
        active = active_jobs(store)
        if (limit and len(active) >= limit) or (
            provider_limit and len([j for j in active if j.get("engine") == engine]) >= provider_limit
        ):
            for job in active:
                refresh(store, registry, job)
    slot_dir = _concurrency.load_config().slot_dir
    with _concurrency._guard(slot_dir):
        # The uniqueness check and the insert happen under ONE hold of the
        # machine-wide guard, so two submits of the same --job-id (threads or
        # processes) cannot both see "absent" and both write: exactly one wins.
        if _store.read_meta(store, job_id) is not None or registry.find(job_id) is not None:
            raise CloudError(f"job id {job_id!r} already exists -- refusing to overwrite its record")
        active = active_jobs(store)
        if limit and len(active) >= limit:
            raise CloudBusy(limit, active)
        same = [j for j in active if j.get("engine") == engine]
        if provider_limit and len(same) >= provider_limit:
            raise CloudBusy(provider_limit, same, provider=engine)
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
    approvals: tuple[str, ...] | list[str] = (),
    routine: str | None = None,
    pr: int | None = None,
    open_pr: bool = False,
    land: bool = False,
    rebase_ok: bool = False,
    model: str | None = None,
    tier: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit ``task`` to the cloud provider for ``engine``; return the job record.

    ``pr`` works on that pull request's head branch (the job records the PR);
    ``open_pr`` records that landing should open a new branch and PR against
    ``branch``; ``land`` makes ``wait`` / ``cloud-sync`` land the result by
    themselves, refusing a moved base unless ``rebase_ok``. ``tier`` and
    ``model`` pick a Claude cloud model (:func:`model_for`). ``extra_meta`` is
    merged into the record (used by cloud reviews).

    Raises :class:`CloudError` (no job recorded) for a missing env mapping,
    a repository without a usable remote, the cloud limit, or a brief that
    asks for a gated action (:data:`GATED_ACTIONS`) not listed in
    ``approvals``; a failure of the submit call itself is recorded as a
    failed job and re-raised.
    """
    import uuid

    if job_id is not None and not JOB_ID_RE.fullmatch(job_id):
        raise CloudError(f"job id must be 8-32 lowercase hex digits or a UUID, got {redact(job_id)[:80]!r}")
    unknown = sorted(set(approvals) - set(GATED_ACTIONS))
    if unknown:
        raise CloudError(f"unknown cloud approval(s) {unknown}: one of {', '.join(GATED_ACTIONS)}")
    missing = [action for action in gated_actions(task) if action not in approvals]
    if missing:
        flags = " ".join(f"--approve-cloud {action}" for action in missing)
        raise CloudError(
            f"the brief asks for gated action(s) {', '.join(missing)}, which a local job's BridgeGate would hold for "
            f"approval; a cloud agent's commands cannot be gated one by one. Run it as a local job, remove the "
            f"request from the brief, or approve up front with {flags}"
        )
    config = config or load_config()
    if pr is not None and open_pr:
        raise CloudError("--pr and --open-pr are alternatives: work on an existing PR's branch, or open a new PR")
    if land and not (pr is not None or open_pr):
        open_pr = True  # landing needs somewhere to go: a new PR is the default
    prov = provider(engine)
    root = repo_root(cwd)
    repo = repo_slug(root)
    env_id = prov.env_for(repo, config, routine)  # first: a repo with no environment fails before anything else
    if not _ENV_ID_RE.fullmatch(env_id):
        raise CloudError(f"env id {env_id!r} for {repo} has unexpected characters")
    chosen_model, model_note = model_for(engine, config, tier=tier, explicit=model, routine=routine)
    pr_info: dict[str, Any] = {}
    if pr is not None:
        from lazytools.code_bridge import _gh

        view = _gh.pr_view(repo, pr)
        if str(view.get("state", "")).upper() != "OPEN":
            raise CloudError(f"PR #{pr} of {repo} is {str(view.get('state', '?')).lower()}, not open")
        if view.get("isCrossRepository"):
            raise CloudError(f"PR #{pr} comes from a fork: a cloud agent cannot work on, or push to, a fork's branch")
        head_ref = str(view.get("headRefName") or "")
        if branch and branch != head_ref:
            raise CloudError(f"--branch {branch!r} conflicts with PR #{pr}, whose head branch is {head_ref!r}: "
                             "drop --branch, the PR's head is the branch the cloud agent works on")
        branch = head_ref
        pr_info = {"pr_number": int(view.get("number") or pr), "pr_url": view.get("url"), "pr_head": head_ref,
                   "pr_base": view.get("baseRefName"), "pr_head_sha_at_submit": view.get("headRefOid")}
    branch = branch or current_branch(root)
    if branch.startswith("-") or _git(root, "check-ref-format", "--branch", branch, check=False) != branch:
        raise CloudError(f"{branch!r} is not a valid branch name")
    job_id = job_id or uuid.uuid4().hex
    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    if _store.read_meta(store, job_id) is not None or registry.find(job_id) is not None:
        # Fast path only (skips the fetch below); _admit re-checks atomically.
        raise CloudError(f"job id {job_id!r} already exists -- refusing to overwrite its record")
    precheck = getattr(prov, "precheck", None)
    if precheck is not None:
        precheck(repo_dir=root, branch=branch, routine=routine)
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
        # Stored and printed redacted; the provider still receives the brief as written.
        "objective": redact(task),
        "cloud_approvals": sorted(set(approvals)),
        "created_at": time.time(),
        "cloud_handle": None,
        "open_pr": bool(open_pr),
        "land": bool(land),
        "rebase_ok": bool(rebase_ok),
        "tier": tier,
        "model": chosen_model,
        "model_note": model_note,
        **pr_info,
        **(extra_meta or {}),
    }
    if engine == "claude":
        from lazytools.code_bridge._cloud_claude import ENV_NOTE, named_branch

        meta["named_result_branch"] = named_branch(task)
        if routine is None:
            meta["environment_note"] = ENV_NOTE
    _admit(store, registry, config, job_id, meta)
    try:
        sub = prov.submit(repo_dir=root, env_id=env_id, branch=branch, prompt=CLOUD_PREAMBLE + task,
                          job_id=job_id, db_path=db_path, routine=routine, config=config, model=chosen_model)
    except BaseException as exc:
        _write(store, registry, job_id, meta, status="failed", error=redact(f"cloud submit failed: {exc}"))
        raise
    # Cancelled while `codex cloud exec` ran: keep "interrupted", but still
    # record the handle so the operator can find and stop the remote task.
    _write_unless_terminal(
        store, registry, job_id,
        {**sub.extra, "cloud_handle": sub.handle, "cloud_url": sub.url, "submitted_at": time.time()},
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
            prov = provider(str(job["engine"]))
            job_diff = getattr(prov, "job_diff", None)
            diff = job_diff(job) if job_diff is not None else prov.diff(str(job["cloud_handle"]))
            if job.get("result_branch"):
                parts.append(f"result branch: {job['result_branch']} (fetched from origin)")
            parts.append(summarize_diff(diff).text())
        except CloudError as exc:
            parts.append(f"diff: unavailable ({exc})")
    if job.get("pushed_sha"):
        parts.append(f"pushed: {job.get('landed_branch')} @ {str(job['pushed_sha'])[:12]}")
    if job.get("pr_url"):
        parts.append(f"pr: {job['pr_url']}")
    if job.get("land_error") and not job.get("landed_at"):
        parts.append(f"landing: not done ({job['land_error']})")
    if job.get("review_comment_url"):
        parts.append(f"review comment: {job['review_comment_url']} (verdict {job.get('review_verdict')})")
    elif job.get("review_note"):
        parts.append(f"review comment: not posted ({job['review_note']})")
    elif job.get("review_posted_at"):
        parts.append(f"review comment: posted (verdict {job.get('review_verdict')})")
    return redact("\n".join(parts))


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
    rebase: bool = False,
    source: str | None = None,
    rebase_flag: str = "--rebase",
) -> dict[str, Any]:
    """Apply a finished cloud task in a NEW git worktree on a NEW branch.

    Never touches the operator's checkout: ``git worktree add -b`` creates
    both, starting from the commit the task started from (``base_commit``,
    recorded at submit). If the target branch's head has moved since, the
    apply is refused unless ``rebase`` is set, in which case the worktree
    starts at the new head instead. Nothing is pushed -- pushing stays a
    separate, approved step.
    """
    if not job.get("cloud_handle"):
        raise CloudError(f"job {job.get('job_id')} has no cloud task to apply")
    if job.get("status") != "done":
        raise CloudError(f"job {job.get('job_id')} is {job.get('status')}, not done: only a finished task can be applied")
    if job.get("applied_worktree"):
        raise CloudError(f"job {job['job_id']} was already applied in {job['applied_worktree']}")
    job_id = str(job["job_id"])
    if not JOB_ID_RE.fullmatch(job_id):
        raise CloudError(f"job id {redact(job_id)[:80]!r} is not hex or a UUID; refusing to derive paths from it")
    root = Path(str(job.get("repo_root") or repo_root(Path(str(job["cwd"])))))
    branch = branch or f"cloud/{job_id[:8]}"
    if branch.startswith("-") or _git(root, "check-ref-format", "--branch", branch, check=False) != branch:
        raise CloudError(f"{branch!r} is not a valid branch name")
    if worktree is None:
        container = worktrees_dir(db_path).expanduser().resolve()
        worktree = (container / job_id).resolve()
        if not _inside(worktree, container) or worktree == container:
            raise CloudError(f"{worktree} is outside {container}")
    else:
        worktree = worktree.expanduser().resolve()
    if worktree.exists():
        raise CloudError(f"{worktree} already exists: remove it or pass --worktree")
    prov = provider(str(job["engine"]))
    if hasattr(prov, "apply_job"):
        return _apply_from_branch(store, registry, prov, job, worktree=worktree, branch=branch, source=source)
    if source is not None:
        raise CloudError("--source is only for Claude cloud jobs (their result is a claude/ branch)")
    base_branch = str(job.get("branch") or "HEAD")
    base_commit = str(job.get("base_commit") or "")
    if not base_commit:
        # No unchecked fallback onto whatever origin/<branch> or the local
        # branch is now: without the recorded base there is no way to tell
        # whether the agent ever saw that tree.
        raise CloudError(
            f"job {job_id} has no recorded base commit (fetching origin/{base_branch} failed when it was "
            "submitted, or the branch was not on the remote), so there is no way to check that the diff applies "
            "to the tree the agent saw; refusing to apply. Re-submit it from a pushed branch, or inspect it with "
            "`lazytools-code-bridge result`"
        )
    head = _remote_base(root, base_branch)
    rebased_from = None
    if head and head != base_commit:
        if not rebase:
            raise CloudError(
                f"origin/{base_branch} has moved from {base_commit[:12]} (the task's base) to {head[:12]} since the "
                f"task was submitted; refusing to apply onto a branch the agent never saw. Pass {rebase_flag} to apply "
                "onto the new head (the diff may not apply cleanly)"
            )
        base, rebased_from = head, base_commit
    elif _git(root, "rev-parse", "--verify", "--quiet", f"{base_commit}^{{commit}}", check=False):
        base = base_commit  # the exact commit the cloud task started from
    else:
        raise CloudError(
            f"the task's base commit {base_commit[:12]} is not in {root} and origin/{base_branch} could not be "
            "fetched; refusing to apply onto an unchecked base. Fetch the branch and retry"
        )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", branch, str(worktree), base)
    try:
        # The provider's diff is the authority for what landing may stage.  In
        # particular, the Codex CLI may create its own untracked files while
        # applying a task; those are not part of the task result.
        diff_files = prov.diff(str(job["cloud_handle"]))
        applied_files = summarize_diff(diff_files).files
        output = prov.apply(str(job["cloud_handle"]), worktree=worktree)
        # Current Codex CLI versions write this diagnostic beside the files
        # they apply.  It can contain account identifiers and is tool output,
        # never task output.
        (worktree / "error.log").unlink(missing_ok=True)
    except BaseException as exc:
        _after_failed_apply(store, registry, root, job_id, worktree, branch, exc)
        raise
    status_lines = _git(worktree, "status", "--porcelain", check=False)
    meta = _store.read_meta(store, job_id) or {}
    meta.update(applied_worktree=str(worktree), applied_branch=branch, applied_base=base,
                applied_files=applied_files, applied_at=time.time())
    _write(store, registry, job_id, meta, status=str(job["status"]), result=job.get("result"), error=job.get("error"))
    return {
        "job_id": job_id,
        "worktree": str(worktree),
        "branch": branch,
        "base": base,
        "changed": [line for line in status_lines.splitlines() if line.strip()],
        "output": output.strip(),
        "pushed": False,
        "rebased_from": rebased_from,
        "base_branch": base_branch,
    }


def _apply_from_branch(store: Any, registry: Any, prov: Any, job: dict[str, Any], *, worktree: Path, branch: str,
                       source: str | None) -> dict[str, Any]:
    """Apply a job whose result is a pushed branch (Claude): check it out in a new worktree; never push."""
    job_id = str(job["job_id"])
    outcome = prov.apply_job(store, registry, job, worktree=worktree, branch=branch, source=source)
    meta = _store.read_meta(store, job_id) or {}
    meta.update(applied_worktree=str(worktree), applied_branch=branch, applied_base=outcome["base"],
                applied_source_branch=outcome["source_branch"], applied_at=time.time())
    _write(store, registry, job_id, meta, status=str(job["status"]), result=job.get("result"), error=job.get("error"))
    return {
        "job_id": job_id,
        "worktree": str(worktree),
        "branch": branch,
        "base": outcome["base"],
        "source_branch": outcome["source_branch"],
        "changed": [],
        "output": redact(outcome.get("output") or ""),
        "pushed": False,
        "rebased_from": None,
        "base_branch": job.get("branch"),
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
    meta.update(apply_failed_worktree=str(worktree), apply_failed_branch=branch, apply_error=redact(str(exc)))
    _store.write_meta(store, job_id, meta)
    if isinstance(exc, CloudError):
        exc.args = (redact(
            f"{exc} -- partial changes kept in {worktree} (branch {branch}); inspect them, then remove that "
            f"worktree and branch (git worktree remove {worktree}; git branch -D {branch}) before retrying, "
            "or retry with --worktree and --branch"),
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
    "DEFAULT_CLOUD_WAIT_TIMEOUT",
    "GATED_ACTIONS",
    "JOB_ID_RE",
    "MAX_CONSECUTIVE_POLL_ERRORS",
    "CLOUD_PREAMBLE",
    "DEFAULT_CLOUD_CONCURRENCY",
    "PROVIDERS",
    "WHERE_CLOUD",
    "WHERE_LOCAL",
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
    "redact_obj",
    "refresh",
    "repo_slug",
    "gated_actions",
    "result_text",
    "submit_job",
    "summarize_diff",
    "wait_job",
]
