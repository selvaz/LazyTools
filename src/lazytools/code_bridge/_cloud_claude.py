"""Claude Code cloud sessions as a code-bridge cloud provider (``--where cloud --engine claude``).

Two submit modes, one result path:

* **CLI mode** (default): ``claude --cloud "<prompt>"`` run from a local
  checkout whose current branch is the requested branch and is already on the
  GitHub ``origin`` at the same commit. The hosted VM clones from GitHub and
  there is no ``--branch`` flag for ``--cloud``, so the bridge checks out the
  branch in a short-lived worktree when the operator's checkout is on another
  branch, verifies it is pushed (the bridge never pushes), runs ``claude``
  there, then removes that worktree. Auth is the claude.ai OAuth login
  (``claude auth login``); API keys are not accepted for cloud sessions.
  The cloud environment is the one selected for the account at claude.ai/code:
  there is no CLI flag to choose it, so the job records that fact instead of
  an environment id.
* **Routine mode** (``--routine NAME``): ``POST /v1/claude_code/routines/{id}/fire``
  with that routine's own bearer token, read from the environment variable
  named in ``[cloud.claude.routines]`` -- the config never holds a token. The
  routine's own settings (repository, branch, environment) apply.

There is **no documented API to poll a cloud session or read its
transcript**. So ``status`` reports ``submitted (no status API): open <url>``
until a result appears in git: a ``claude/...`` branch on ``origin`` that did
not exist (or pointed elsewhere) when the job was submitted, or the branch the
brief named. That branch is the job's result: ``result`` summarises its diff
against the base commit and ``apply`` checks it out in a NEW worktree on a
NEW local branch, never pushing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _cloud
from lazytools.code_bridge._cloud import (
    CLI_TIMEOUT,
    CloudConfig,
    CloudError,
    CloudStatus,
    Submission,
    _git,
    _remote_base,
    redact,
)

#: What a CLI-mode job records as its environment: the hosted environment is
#: picked in the claude.ai UI, never on the command line.
ENV_LABEL = "claude.ai-default"
ENV_NOTE = (
    "the cloud environment is the one selected for this account at claude.ai/code (setup script, network "
    "access); claude --cloud has no flag to choose it"
)
ROUTINE_API = "https://api.anthropic.com/v1/claude_code/routines/{routine_id}/fire"
ROUTINE_API_VERSION = "2026-10-21"
RESULT_PREFIX = "claude/"
#: A result branch named in the brief ("push to claude/fix-login").
_NAMED_BRANCH = re.compile(r"(?<![\w/.-])claude/[A-Za-z0-9._/-]*[A-Za-z0-9_]")
_SESSION_URL = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_./?=&-]+")
_SESSION_ID = re.compile(r"\b(session_[A-Za-z0-9_-]{6,})\b")
NO_STATUS_API = "submitted (no status API): open {url}"


def _claude_argv() -> list[str]:
    """The command prefix that runs the Claude Code CLI with no shell in between.

    ``CLAUDE_BIN`` first, then ``PATH``. A Windows ``.cmd``/``.bat`` npm shim
    is never executed (``cmd.exe`` would re-parse the brief): the package's
    ``cli.js`` beside it is run with ``node`` instead, or the call is refused.
    """
    found = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not found:
        raise CloudError("Claude Code CLI not found: install it (https://claude.com/claude-code), or set CLAUDE_BIN")
    if Path(found).suffix.lower() not in (".cmd", ".bat"):
        return [found]
    shim_dir = Path(found).parent
    script = shim_dir / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node = shim_dir / "node.exe"
    node_path = str(node) if node.is_file() else shutil.which("node")
    if script.is_file() and node_path:
        return [node_path, str(script)]
    raise CloudError(
        f"Claude Code CLI resolves to the batch shim {found}, which would pass the brief through cmd.exe; "
        "set CLAUDE_BIN to claude.exe (the native installer's binary)"
    )


def parse_cli_submit(text: str) -> tuple[str | None, str | None, str]:
    """``(session id, session url, raw line)`` from ``claude --cloud`` output.

    Tolerant on purpose: a JSON line with ``session_id``/``url`` (the shape
    documented for follow-ups, ``{ok, session_id, url}``) wins; otherwise a
    claude.ai session URL or a ``session_...`` id anywhere in the text.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("ok") is False:
            raise CloudError(f"claude --cloud reported failure: {line[:2000]}")
        sid = payload.get("session_id") or payload.get("sessionId") or payload.get("claude_code_session_id")
        url = payload.get("url") or payload.get("session_url") or payload.get("claude_code_session_url")
        if sid or url:
            return (str(sid) if sid else None), (str(url) if url else None), line
    url_match = _SESSION_URL.search(text)
    id_match = _SESSION_ID.search(text)
    if url_match or id_match:
        line = next((ln.strip() for ln in text.splitlines() if (url_match and url_match.group(0) in ln)
                     or (id_match and id_match.group(1) in ln)), text.strip())
        return (id_match.group(1) if id_match else None), (url_match.group(0).rstrip(".,;)") if url_match else None), line
    raise CloudError(f"claude --cloud printed no session id or URL; output was:\n{text.strip()[:2000]}")


def _remote_claude_refs(root: Path) -> dict[str, str]:
    """``{branch: sha}`` for every ``claude/*`` branch on ``origin`` (one ``ls-remote``)."""
    out = _git(root, "ls-remote", "--heads", "origin", f"refs/heads/{RESULT_PREFIX}*")
    refs: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            refs[ref[len("refs/heads/"):]] = sha.strip()
    return refs


def _fetch_branch(root: Path, branch: str) -> str:
    _git(root, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    return _git(root, "rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}")


def named_branch(brief: str) -> str | None:
    """The ``claude/...`` result branch a brief names, if exactly one."""
    names = sorted({m.group(0) for m in _NAMED_BRANCH.finditer(brief)})
    return names[0] if len(names) == 1 else None


def verify_pushed(root: Path, branch: str) -> str:
    """``origin``'s tip of ``branch``; refuses (never pushes) when it is missing or the local branch differs."""
    remote = _remote_base(root, branch)
    push_hint = f"push it first (git -C \"{root}\" push -u origin {branch}); the bridge never pushes for you"
    if remote is None:
        raise CloudError(f"branch {branch!r} is not on origin, and the Claude cloud VM clones from GitHub: {push_hint}")
    current = _git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if current == branch:
        head = _git(root, "rev-parse", "HEAD")
        if head != remote:
            raise CloudError(
                f"local {branch} ({head[:12]}) differs from origin/{branch} ({remote[:12]}): the cloud session would "
                f"not see the same commit; {push_hint} (or pull)"
            )
        return remote
    local = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False)
    if local and local != remote:
        raise CloudError(
            f"local {branch} ({local[:12]}) differs from origin/{branch} ({remote[:12]}): {push_hint} (or pull)"
        )
    return remote


def _checkout_for(root: Path, branch: str, worktree: Path) -> tuple[Path, bool]:
    """A directory whose current branch is ``branch`` at the commit ``origin`` has.

    Returns ``(dir, created)``; ``created`` worktrees are removed after submit.
    """
    verify_pushed(root, branch)
    if _git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False) == branch:
        return root, False
    local = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if local:
        _git(root, "worktree", "add", str(worktree), branch)
    else:
        _git(root, "worktree", "add", "--track", "-b", branch, str(worktree), f"origin/{branch}")
    return worktree, True


def _http_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, str]:
    """POST and return ``(status, body)``; HTTP errors are returned, not raised. Patched in tests."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise CloudError(f"routine fire request failed: {exc}") from exc


class ClaudeCloud:
    """Claude Code cloud sessions: ``claude --cloud`` or a routine's fire endpoint."""

    name = "claude"

    def env_for(self, repo: str, config: CloudConfig, routine: str | None = None) -> str:
        if routine is None:
            return ENV_LABEL
        if routine not in config.routines:
            known = ", ".join(sorted(config.routines)) or "none configured"
            raise CloudError(
                f"no routine {routine!r} in [cloud.claude.routines] of {config.path} (known: {known}). Add:\n\n"
                f'    [cloud.claude.routines.{routine}]\n    id = "trig_..."\n'
                f'    token_env = "LAZYTOOLS_ROUTINE_TOKEN"\n    repo = "owner/name"\n    branch = "main"'
            )
        configured = config.routines[routine]["repo"]
        if configured.lower() != repo.lower():
            raise CloudError(
                f"routine {routine!r} is configured for repository {configured}, not {repo}; "
                "use a checkout of that repository or correct the routine configuration"
            )
        return f"routine:{routine}"

    # -- submit ------------------------------------------------------------ #

    def precheck(self, *, repo_dir: Path, branch: str, routine: str | None,
                 config: CloudConfig | None = None) -> None:
        """Refusals that must record no job: CLI mode needs the branch pushed as-is."""
        if routine is None:
            verify_pushed(repo_dir, branch)
        else:
            expected = (config or _cloud.load_config()).routines[routine]["branch"]
            if branch != expected:
                raise CloudError(
                    f"routine {routine!r} is configured for branch {expected!r}, not {branch!r}; "
                    "pass that branch or correct the routine configuration"
                )

    def submit(self, *, repo_dir: Path, env_id: str, branch: str, prompt: str, job_id: str = "",
               db_path: Path | None = None, routine: str | None = None, config: CloudConfig | None = None,
               **_: Any) -> Submission:
        refs_before = _remote_claude_refs(repo_dir)
        requested_branch = named_branch(prompt)
        result_branch = requested_branch or f"{RESULT_PREFIX}lazytools-{job_id}"
        if requested_branch is None:
            prompt += (
                f"\n\nPush the completed result to the exact branch `{result_branch}`. "
                "Do not use any other result branch."
            )
        if routine is not None:
            sid, url, raw = self._fire_routine(routine, prompt, config or _cloud.load_config())
            mode = "routine"
        else:
            worktree = _cloud.worktrees_dir(db_path) / f"{job_id or 'submit'}-submit"
            checkout, created = _checkout_for(repo_dir, branch, worktree)
            try:
                sid, url, raw = self._run_cli(checkout, prompt)
            finally:
                if created:
                    _git(repo_dir, "worktree", "remove", str(checkout), check=False)
            mode = "cli"
        handle = url or sid
        if not handle:
            raise CloudError("the Claude cloud submit returned neither a session id nor a URL")
        return Submission(
            handle=handle, url=url, env_id=env_id, raw=redact(raw),
            extra={
                "cloud_mode": mode,
                "routine": routine,
                "claude_session_id": sid,
                "claude_submit_raw": redact(raw)[:4000],
                "claude_refs_before": refs_before,
                "claude_result_branch": result_branch,
                "environment_note": ENV_NOTE if mode == "cli" else "the routine's own environment applies",
            },
        )

    def _run_cli(self, cwd: Path, prompt: str) -> tuple[str | None, str | None, str]:
        argv = _claude_argv()
        last = ""
        for extra in (["--output-format", "json"], []):
            try:
                proc = subprocess.run(
                    [*argv, "--cloud", prompt, *extra], cwd=str(cwd), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT, stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as exc:
                raise CloudError(f"claude --cloud timed out after {CLI_TIMEOUT:g}s") from exc
            except OSError as exc:
                raise CloudError(f"could not start the Claude Code CLI: {exc}") from exc
            text = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            if proc.returncode == 0:
                return parse_cli_submit(proc.stdout or text)
            last = redact(text.strip())
            # An older CLI that rejects --output-format with --cloud: retry once without it.
            if extra and re.search(r"(?i)output-format|unknown option|unexpected argument", text):
                continue
            break
        hint = ""
        if re.search(r"(?i)\b(log ?in|auth|credential|unauthori[sz]ed|expired)\b", last):
            hint = " -- run `claude auth login` (claude.ai login; API keys are not accepted for cloud sessions)"
        raise CloudError(f"claude --cloud failed: {last[:2000]}{hint}")

    def _fire_routine(self, name: str, prompt: str, config: CloudConfig) -> tuple[str | None, str | None, str]:
        entry = config.routines.get(name)
        if entry is None:
            raise CloudError(f"no routine {name!r} in [cloud.claude.routines]")
        token = os.environ.get(entry["token_env"], "").strip()
        if not token:
            raise CloudError(
                f"routine {name!r}: environment variable {entry['token_env']} is not set (it must hold the routine's "
                "API trigger token; the token is never read from the config file)"
            )
        status, body = _http_post(
            ROUTINE_API.format(routine_id=entry["id"]),
            {
                "Authorization": f"Bearer {token}",
                "anthropic-version": ROUTINE_API_VERSION,
                "content-type": "application/json",
            },
            json.dumps({"text": prompt}).encode("utf-8"),
            CLI_TIMEOUT,
        )
        body = redact(body.replace(token, "[redacted]"))
        if not 200 <= status < 300:
            raise CloudError(f"routine {name!r} fire returned HTTP {status}: {body[:2000]}")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise CloudError(f"routine {name!r} fire returned no JSON object: {body[:2000]}")
        sid = payload.get("claude_code_session_id")
        url = payload.get("claude_code_session_url")
        if not sid and not url:
            raise CloudError(f"routine {name!r} fire returned no session: {body[:2000]}")
        return (str(sid) if sid else None), (str(url) if url else None), body.strip()

    # -- status / result from git ----------------------------------------- #

    # The handle-only protocol methods cannot work without a status API; the
    # bridge calls the job-aware hooks below (job_status, job_diff, apply_job).
    def status(self, handle: str) -> CloudStatus:
        raise CloudError("Claude cloud sessions have no status API; use job_status(job)")

    def diff(self, handle: str) -> str:
        raise CloudError("Claude cloud sessions have no diff API; use job_diff(job)")

    def apply(self, handle: str, *, worktree: Path) -> str:
        raise CloudError("Claude cloud results are applied from their claude/ branch; use apply_job(...)")

    def job_status(self, job: dict[str, Any], *, claimed: set[str] | frozenset[str] = frozenset()) -> CloudStatus:
        """No status API: look for the job's result branch on ``origin``."""
        url = str(job.get("cloud_url") or job.get("cloud_handle") or "the claude.ai session list")
        root = Path(str(job.get("repo_root") or job.get("cwd")))
        candidates = self.result_candidates(job, root, claimed=claimed)
        if not candidates:
            text = NO_STATUS_API.format(url=url)
            return CloudStatus("running", text, "no claude/ result branch on origin yet", text)
        newest, sha = candidates[0]
        others = [name for name, _ in candidates[1:]]
        raw = f"{newest} pushed (no status API; the session may still be working): open {url}"
        message = f"result branch {newest} at {sha[:12]}; session {url}"
        if others:
            message += f"\nother new claude/ branches: {', '.join(others)} (pick one with `apply --source`)"
        return CloudStatus("done", raw, message, raw,
                           extra={"result_branch": newest, "result_commit": sha, "result_branch_candidates": others})

    def result_candidates(self, job: dict[str, Any], root: Path, *,
                          claimed: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str]]:
        """New ``claude/`` branches for this job, newest commit first, as ``(branch, sha)``."""
        named = job.get("claude_result_branch") or job.get("named_result_branch")
        refs = _remote_claude_refs(root)
        before = job.get("claude_refs_before")
        if named:
            fresh = named in refs and (not isinstance(before, dict) or before.get(named) != refs[named])
            names = [named] if fresh and named not in claimed else []
        else:
            names = [b for b, sha in refs.items()
                     if b not in claimed and (not isinstance(before, dict) or before.get(b) != sha)]
        submitted = float(job.get("submitted_at") or job.get("created_at") or 0)
        found: list[tuple[float, str, str]] = []
        for branch in names:
            sha = _fetch_branch(root, branch)
            when = float(_git(root, "log", "-1", "--format=%ct", sha) or 0)
            if not isinstance(before, dict) and when < submitted - 300:
                continue  # no snapshot at submit: fall back to "committed after submit time"
            found.append((when, branch, sha))
        found.sort(reverse=True)
        return [(branch, sha) for _, branch, sha in found]

    def job_diff(self, job: dict[str, Any], source: str | None = None) -> str:
        branch = source or job.get("result_branch")
        if not branch:
            url = job.get("cloud_url") or job.get("cloud_handle")
            raise CloudError(f"no claude/ result branch on origin yet -- {NO_STATUS_API.format(url=url)}")
        root = Path(str(job.get("repo_root") or job.get("cwd")))
        tip = _fetch_branch(root, str(branch))
        base = str(job.get("base_commit") or "")
        if not base or not _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False):
            base = _git(root, "merge-base", tip, f"refs/remotes/origin/{job.get('branch') or 'HEAD'}", check=False)
        if not base:
            raise CloudError(f"cannot find a base to diff {branch} against")
        return _git(root, "diff", f"{base}...{tip}")

    def apply_job(self, store: Any, registry: Any, job: dict[str, Any], *, worktree: Path, branch: str,
                  source: str | None = None) -> dict[str, Any]:
        """Check the result branch out in a NEW worktree on a NEW local branch; never push."""
        source = source or job.get("result_branch")
        if not source:
            raise CloudError(
                f"job {job.get('job_id')}: no claude/ result branch on origin yet; open "
                f"{job.get('cloud_url') or job.get('cloud_handle')} and retry once the session pushed its branch"
            )
        allowed = {str(x) for x in (job.get("claude_result_branch"), job.get("result_branch")) if x}
        if str(source) not in allowed:
            expected = job.get("claude_result_branch") or job.get("result_branch")
            raise CloudError(f"--source {source!r} does not belong to this job (expected {expected!r})")
        root = Path(str(job.get("repo_root") or job.get("cwd")))
        tip = _fetch_branch(root, str(source))
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(root, "worktree", "add", "-b", branch, str(worktree), tip)
        diff = summarize(root, str(job.get("base_commit") or ""), tip)
        return {"source_branch": source, "base": tip, "output": diff}


def summarize(root: Path, base: str, tip: str) -> str:
    if not base or not _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False):
        return ""
    return _cloud.summarize_diff(_git(root, "diff", f"{base}...{tip}")).text()


__all__ = ["ENV_LABEL", "ENV_NOTE", "NO_STATUS_API", "ROUTINE_API", "ROUTINE_API_VERSION", "ClaudeCloud",
           "named_branch", "parse_cli_submit"]

