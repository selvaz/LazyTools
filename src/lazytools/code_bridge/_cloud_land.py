"""Landing, independent reviews and reconciliation for cloud jobs.

A finished cloud job is only a diff (Codex Cloud) or a ``claude/...`` branch
(Claude Code cloud). This module turns it into a pushed branch and a pull
request without a person in the loop:

* :func:`land_job` -- commit the applied diff in its worktree, push it, then
  update the recorded PR or open a new one (``gh``). A provider that pushes by
  itself (Claude) is verified and recorded instead. It refuses a moved base
  unless the job allows a rebase, and it never force-pushes: the only push
  forms used are ``git push origin <sha-or-HEAD>:refs/heads/<branch>``, which
  git itself rejects when it is not a fast-forward.
* :func:`prepare_review` / :func:`post_review` -- a review job run by the
  OPPOSITE engine of the reviewed job's author, posted to the PR as exactly one
  comment in the ``independent-review v1`` format.
* :func:`process_job` / :func:`sync` -- what ``wait`` and ``cloud-sync`` run on
  a finished job: land it, or post its review, or record why it failed.

Landing and posting are idempotent and resumable: each step records what it
did in the job meta (``land_commit``, ``pushed_sha``, ``pr_url``,
``review_posted_at``), and a claim stamp keeps two schedulers from landing the
same job at once.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _cloud, _concurrency, _gh, _jobs, _store
from lazytools.code_bridge._cloud import CloudError, redact

REVIEW_MARKER = "<!-- independent-review v1 -->"
REVIEW_FILE = "REVIEW.md"
#: A claim older than this is abandoned (the process holding it died).
CLAIM_STALE_SECONDS = 900.0
#: A review whose REVIEW.md cannot be read is retried this many times, then given up.
MAX_REVIEW_ATTEMPTS = 3
_MAX_COMMENT_BODY = 60000
FORBIDDEN_PUSH_ARGS = frozenset({"--force", "-f", "--force-with-lease", "--mirror", "--delete", "-d", "--prune"})


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def opposite_engine(engine: str) -> str:
    """The reviewer for ``engine``'s work: the other engine (same rule as the local router)."""
    others = [name for name in _jobs.ENGINES if name != engine]
    if engine not in _jobs.ENGINES or len(others) != 1:
        raise CloudError(f"no opposite engine for {engine!r}")
    return others[0]


def job_title(job: dict[str, Any]) -> str:
    """A one-line title from the brief: its first non-empty line, markdown marks stripped, 72 chars."""
    for line in str(job.get("objective") or "").splitlines():
        line = line.strip().lstrip("#*-> ").strip()
        if line:
            return redact(line[:72].rstrip())
    return f"Cloud job {str(job.get('job_id', ''))[:8]}"


def _summary(job: dict[str, Any]) -> str:
    """The provider's own summary of what it did (Codex); Claude's message is only a pointer."""
    if job.get("engine") == "claude":
        return ""
    return redact(str(job.get("result") or "").strip())[:600]


def commit_message(job: dict[str, Any]) -> tuple[str, str]:
    """``(subject, body)`` for the landing commit: no attribution, no trailers."""
    body = f"Cloud job {str(job.get('job_id', ''))[:8]} ({job.get('engine')})."
    summary = _summary(job)
    if summary:
        body += f"\n\n{summary}"
    return job_title(job), body


def pr_body(job: dict[str, Any]) -> str:
    lines = [f"Cloud job `{str(job.get('job_id', ''))[:8]}` ({job.get('engine')}), submitted by lazytools-code-bridge.", ""]
    brief = redact(str(job.get("objective") or "").strip())
    if brief:
        lines += ["Brief:", "", *(f"> {ln}" for ln in brief[:1500].splitlines()), ""]
    summary = _summary(job)
    if summary:
        lines += ["Agent summary:", "", summary, ""]
    return "\n".join(lines).rstrip() + "\n"


def _update_meta(store: Any, job_id: str, updates: dict[str, Any], drop: tuple[str, ...] = ()) -> dict[str, Any]:
    with _concurrency._guard(_concurrency.load_config().slot_dir):
        meta = _store.read_meta(store, job_id) or {}
        meta.update(updates)
        for key in drop:
            meta.pop(key, None)
        meta["updated_at"] = time.time()
        _store.write_meta(store, job_id, meta)
        return meta


def _claim(store: Any, job_id: str, key: str) -> bool:
    """Take the ``key`` claim on a job unless a live one exists. Atomic under the machine-wide guard."""
    with _concurrency._guard(_concurrency.load_config().slot_dir):
        meta = _store.read_meta(store, job_id) or {}
        started = meta.get(f"{key}_claimed_at")
        if started and time.time() - float(started) < CLAIM_STALE_SECONDS:
            return False
        meta[f"{key}_claimed_at"] = time.time()
        _store.write_meta(store, job_id, meta)
    return True


def _release(store: Any, job_id: str, key: str) -> None:
    _update_meta(store, job_id, {}, drop=(f"{key}_claimed_at",))


def _check_branch_name(root: Path, branch: str) -> None:
    if branch.startswith("-") or _cloud._git(root, "check-ref-format", "--branch", branch, check=False) != branch:
        raise CloudError(f"{branch!r} is not a valid branch name")


def _push(cwd: Path, source: str, branch: str) -> None:
    """Push ``source`` (a sha or HEAD) to ``refs/heads/<branch>`` on origin. Fast-forward only, always."""
    refspec = f"{source}:refs/heads/{branch}"
    argv = ["push", "origin", refspec]
    # Belt and braces: nothing above can build a forced push, and this keeps it so.
    assert not (FORBIDDEN_PUSH_ARGS & set(argv)) and not refspec.startswith("+")
    try:
        _cloud._git(cwd, *argv, timeout=300)
    except CloudError as exc:
        raise CloudError(f"push of {branch} was rejected or failed (the bridge never force-pushes): {exc}") from exc


def _remote_tip(root: Path, branch: str) -> str | None:
    out = _cloud._git(root, "ls-remote", "--heads", "origin", f"refs/heads/{branch}", check=False)
    return out.split()[0] if out.split() else None


def _outcome(job: dict[str, Any], *, already: bool = False, note: str | None = None) -> dict[str, Any]:
    return {
        "job_id": str(job["job_id"]),
        "landed": bool(job.get("landed_at")),
        "already": already,
        "branch": job.get("landed_branch"),
        "pushed_sha": job.get("pushed_sha"),
        "pr_url": job.get("pr_url"),
        "pr_number": job.get("pr_number"),
        "worktree": job.get("applied_worktree"),
        "base_branch": job.get("pr_base") or job.get("branch"),
        "note": note or job.get("land_note"),
    }


# --------------------------------------------------------------------------- #
# landing
# --------------------------------------------------------------------------- #


def land_job(store: Any, registry: Any, job: dict[str, Any], *, db_path: Path | None = None,
             rebase_ok: bool | None = None, branch: str | None = None, worktree: Path | None = None,
             source: str | None = None) -> dict[str, Any]:
    """Land a finished cloud job: commit, push, update or open its PR. Idempotent."""
    job_id = str(job["job_id"])
    job = _cloud._merged(store, registry, job_id)
    if job.get("landed_at"):
        return _outcome(job, already=True)
    if isinstance(job.get("review"), dict):
        raise CloudError(f"job {job_id[:8]} is a review: its verdict is posted to the PR, there is nothing to land")
    if job.get("status") != "done" or not job.get("cloud_handle"):
        raise CloudError(f"job {job_id[:8]} is {job.get('status')}, not done: only a finished task can be landed")
    allow_rebase = bool(job.get("rebase_ok")) if rebase_ok is None else bool(rebase_ok)
    if not _claim(store, job_id, "land"):
        raise CloudError(f"job {job_id[:8]} is being landed by another process right now; retry in a minute")
    try:
        prov = _cloud.provider(str(job["engine"]))
        if hasattr(prov, "apply_job"):
            outcome = _land_pushed_branch(store, registry, job, source=source, rebase_ok=allow_rebase)
        else:
            if source is not None:
                raise CloudError("--source is only for Claude cloud jobs (their result is a claude/ branch)")
            outcome = _land_diff(store, registry, job, db_path=db_path, rebase_ok=allow_rebase, branch=branch,
                                 worktree=worktree)
        return outcome
    except CloudError as exc:
        _update_meta(store, job_id, {"land_error": redact(str(exc)), "land_failed_at": time.time()})
        raise
    finally:
        _release(store, job_id, "land")


def _base_moved_message(job: dict[str, Any], head: str, base: str) -> str:
    return (f"origin/{job.get('branch')} has moved from {base[:12]} (the task's base) to {head[:12]} since the task was "
            "submitted; refusing to land onto a branch the agent never saw. Re-submit the job, or allow it with "
            "--rebase-ok (the diff is then applied onto the new head and may not apply cleanly)")


def _check_recorded_pr(job: dict[str, Any], root: Path, *, rebase_ok: bool) -> None:
    """Refuse a recorded PR whose head identity or recorded starting commit moved."""
    if not job.get("pr_number") or rebase_ok:
        return
    view = _gh.pr_view(str(job["repo"]), int(job["pr_number"]))
    recorded_head = str(job.get("pr_head") or "")
    live_head = str(view.get("headRefName") or "")
    if live_head != recorded_head:
        raise CloudError(
            f"PR #{job['pr_number']} head branch moved from {recorded_head!r} to {live_head!r} since submission; "
            "refusing to push. Re-submit the job, or allow it with --rebase-ok"
        )
    recorded_base = str(job.get("base_commit") or job.get("pr_head_sha_at_submit") or "")
    live_base = _cloud._remote_base(root, recorded_head)
    # A resumed landing may see the commit that this job already pushed.
    expected = {recorded_base, str(job.get("pushed_sha") or "")}
    if live_base and live_base not in expected:
        raise CloudError(_base_moved_message(job, live_base, recorded_base))


def _ensure_pr(store: Any, job: dict[str, Any], *, head: str, base: str, title: str) -> tuple[str, int | None]:
    """The job's PR url and number: the recorded PR (verified), an open PR for ``head``, or a new one."""
    repo = str(job["repo"])
    if job.get("pr_number"):
        view = _gh.pr_view(repo, int(job["pr_number"]))
        if str(view.get("state", "")).upper() != "OPEN":
            raise CloudError(f"PR #{job['pr_number']} is {str(view.get('state', '?')).lower()}: the commit was pushed to "
                             f"{head}, but there is no open PR to update")
        return str(view["url"]), int(view["number"])
    existing = _gh.pr_for_branch(repo, head)
    if existing:
        return str(existing["url"]), int(existing["number"])
    url = _gh.pr_create(repo, base=base, head=head, title=title, body=pr_body(job))
    return url, _gh.pr_number_from_url(url)


def _land_diff(store: Any, registry: Any, job: dict[str, Any], *, db_path: Path | None, rebase_ok: bool,
               branch: str | None, worktree: Path | None) -> dict[str, Any]:
    """Codex Cloud: apply the diff in a worktree, commit, push, PR."""
    job_id = str(job["job_id"])
    id8 = job_id[:8]
    root = Path(str(job.get("repo_root") or _cloud.repo_root(Path(str(job["cwd"])))))
    base_branch = str(job.get("branch") or "")
    pr_number = job.get("pr_number")
    remote_branch = str(job["pr_head"]) if pr_number else (branch or f"cloud/{id8}")
    pr_base = str(job.get("pr_base") or base_branch) if pr_number else base_branch
    _check_branch_name(root, remote_branch)
    local_branch = branch or f"cloud/{id8}"

    created = False
    wt_text = job.get("applied_worktree")
    if wt_text and not Path(str(wt_text)).is_dir():
        job = {**job, **_update_meta(store, job_id, {}, drop=("applied_worktree", "applied_branch", "applied_base"))}
        wt_text = None
    if wt_text:
        wt, base = Path(str(wt_text)), str(job.get("applied_base") or "")
        head = _cloud._remote_base(root, base_branch)
        # Once the commit is pushed, the branch has moved because of this job's own push: not a foreign move.
        if head and base and head != base and not rebase_ok and not job.get("pushed_sha"):
            raise CloudError(_base_moved_message(job, head, base))
    else:
        applied = _cloud.apply_job(store, registry, job, db_path=db_path, branch=local_branch, worktree=worktree,
                                   rebase=rebase_ok, rebase_flag="--rebase-ok")
        wt, base = Path(applied["worktree"]), str(applied["base"])
        created = worktree is None  # a worktree this landing made is cleaned up after it; a chosen one is kept
        job = _cloud._merged(store, registry, job_id)

    sha = str(job.get("land_commit") or "")
    if not sha:
        changed_files = [str(name) for name in job.get("applied_files") or []]
        status = _cloud._git(wt, "status", "--porcelain", "-z", "--untracked-files=all", check=False)
        unexpected = []
        for entry in status.split("\0"):  # -z: unquoted names, so spaces and non-ASCII compare correctly
            if entry.startswith("?? ") and entry[3:] not in changed_files:
                unexpected.append(entry[3:])
        if unexpected:
            names = ", ".join(repr(name) for name in unexpected)
            raise CloudError(f"unexpected untracked file(s) remain after cloud apply: {names}; refusing to land")
        if changed_files and status:
            _cloud._git(wt, "add", "--", *changed_files)
            # Tracked changes only (never untracked files): picks up the source side of a provider-reported rename.
            _cloud._git(wt, "add", "-u")
        if _cloud._git(wt, "status", "--porcelain", check=False):
            subject, body = commit_message(job)
            message_args = ["-m", subject, *(["-m", body] if body else [])]
            _cloud._git(wt, "commit", "-q", *message_args)
        sha = _cloud._git(wt, "rev-parse", "HEAD")
        if sha == base:
            note = "the cloud task produced no changes: nothing to commit, push or open a PR for"
            meta = _update_meta(store, job_id, {"landed_at": time.time(), "land_note": note, "land_error": None})
            return _outcome({**job, **meta}, note=note)
        _update_meta(store, job_id, {"land_commit": sha})

    tip = _remote_tip(root, remote_branch)
    if tip != sha:
        # Re-read immediately before the only operation that can publish data.
        _check_recorded_pr(job, root, rebase_ok=rebase_ok)
        # A new-branch landing never takes over a branch someone else owns.
        if tip and not pr_number and not _is_ancestor(root, tip, sha):
            raise CloudError(f"branch {remote_branch} already exists on origin at {tip[:12]} and is not an "
                             "ancestor of this job's commit; refusing to overwrite it (pass --branch to land "
                             "under another name)")
        _push(wt, "HEAD", remote_branch)
        if _remote_tip(root, remote_branch) != sha:
            raise CloudError(f"pushed {remote_branch} but origin does not show {sha[:12]} as its tip; not recording it")
    _update_meta(store, job_id, {"pushed_sha": sha, "landed_branch": remote_branch})
    job = _cloud._merged(store, registry, job_id)

    url, number = _ensure_pr(store, job, head=remote_branch, base=pr_base, title=job_title(job))
    meta = _update_meta(store, job_id, {"landed_at": time.time(), "pr_url": url, "pr_number": number,
                                         "land_error": None, "land_note": None})
    if created:
        # The work is on origin now. Removing the worktree also frees its branch for a later checkout (a review).
        _cloud._git(root, "worktree", "remove", "--force", str(wt), check=False)
    return {**_outcome({**job, **meta}), "worktree_removed": created}


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    import subprocess

    proc = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def _land_pushed_branch(store: Any, registry: Any, job: dict[str, Any], *, source: str | None,
                        rebase_ok: bool) -> dict[str, Any]:
    """Claude cloud pushed its own claude/ branch: verify it, then point the PR at it and record it."""
    from lazytools.code_bridge._cloud_claude import _fetch_branch

    job_id = str(job["job_id"])
    root = Path(str(job.get("repo_root") or _cloud.repo_root(Path(str(job["cwd"])))))
    result_branch = source or job.get("result_branch")
    if not result_branch:
        raise CloudError(f"job {job_id[:8]}: no claude/ result branch on origin yet; the session has not pushed one")
    allowed = {str(x) for x in (job.get("claude_result_branch"), job.get("result_branch")) if x}
    if str(result_branch) not in allowed:
        expected = job.get("claude_result_branch") or job.get("result_branch")
        raise CloudError(f"--source {result_branch!r} does not belong to this job (expected {expected!r})")
    tip = _fetch_branch(root, str(result_branch))  # verifies the branch exists on origin
    base_branch = str(job.get("branch") or "")
    base_commit = str(job.get("base_commit") or "")
    head = _cloud._remote_base(root, base_branch)
    if head and base_commit and head != base_commit and not rebase_ok and not job.get("pushed_sha"):
        raise CloudError(_base_moved_message(job, head, base_commit))
    pr_number = job.get("pr_number")
    landed_branch = str(result_branch)
    if pr_number:
        pr_head = str(job["pr_head"])
        pr_tip = _cloud._remote_base(root, pr_head)
        if pr_tip != tip:
            if not pr_tip or not _is_ancestor(root, pr_tip, tip):
                raise CloudError(
                    f"{result_branch} ({tip[:12]}) is not a fast-forward of PR #{pr_number}'s head {pr_head} "
                    f"({(pr_tip or '?')[:12]}): the PR head moved, or the session did not build on it. The work is on "
                    f"{result_branch}; merge it into {pr_head} by hand (the bridge never force-pushes)")
            _push(root, tip, pr_head)
            if _remote_tip(root, pr_head) != tip:
                raise CloudError(f"pushed {pr_head} but origin does not show {tip[:12]} as its tip; not recording it")
        landed_branch = pr_head
    _update_meta(store, job_id, {"pushed_sha": tip, "landed_branch": landed_branch, "result_commit": tip})
    job = _cloud._merged(store, registry, job_id)
    url, number = _ensure_pr(store, job, head=landed_branch, base=str(job.get("pr_base") or base_branch),
                             title=job_title(job))
    meta = _update_meta(store, job_id, {"landed_at": time.time(), "pr_url": url, "pr_number": number,
                                         "land_error": None, "land_note": None})
    return _outcome({**job, **meta})


# --------------------------------------------------------------------------- #
# independent cloud review
# --------------------------------------------------------------------------- #


def review_brief(*, branch: str, head: str, base: str, author: str, job_id: str, reviewer: str, extra: str = "") -> str:
    lines = [
        "Independent code review. This is a READ-ONLY task: do not change any existing file.",
        "",
        f"Review the changes on branch `{branch}` (head {head}) of this repository, compared with `{base}`. "
        f"Inspect them with `git diff origin/{base}...{head}` (the exact commit under review) and read the surrounding "
        "code as needed. "
        f"The change was written by {author}; you are the independent second pair of eyes.",
        "Look for correctness bugs, regressions, unsafe behaviour, missing or weak tests, and anything that should "
        "stop the change from being accepted. Be specific: cite file and line.",
        "",
        f"Write your review to a NEW file named {REVIEW_FILE} at the repository root: it is the only file you may "
        "create. Give a short summary, then the findings, each with a severity (blocking or non-blocking), file:line, "
        "and what is wrong and why. The LAST line of the file must be exactly `verdict: blocking` (at least one "
        "blocking finding) or `verdict: no-blocking`.",
    ]
    if reviewer == "claude":
        lines.append(f"Do your work on a new branch named claude/review-{job_id[:8]}.")
    if extra.strip():
        lines += ["", extra.strip()]
    return "\n".join(lines)


def prepare_review(store: Any, registry: Any, reviewed_ref: str, *, job_id: str, repo: str | None = None,
                   engine: str | None = None, extra: str = "", root: Path | None = None) -> dict[str, Any]:
    """Everything ``run --where cloud --review-of`` needs: reviewer engine, branch to run on, brief, review meta."""
    reviewed = _jobs.find_job(store, registry, reviewed_ref)
    if reviewed is None:
        raise CloudError(f"no job found matching {reviewed_ref!r}")
    if not _cloud.is_cloud(reviewed):
        raise CloudError(f"job {str(reviewed['job_id'])[:8]} is a local job: a cloud review needs a cloud job whose "
                         "branch is pushed (use a local `run --tier ... --review-of` for local jobs)")
    if _cloud.is_cloud(reviewed) and reviewed.get("status") in _cloud.ACTIVE_STATUSES:
        reviewed = _cloud.refresh(store, registry, reviewed)
    if isinstance(reviewed.get("review"), dict):
        raise CloudError("a review is not itself reviewed: pass the job that wrote the code")
    author = str(reviewed["engine"])
    reviewer = opposite_engine(author)
    if engine is not None and engine != reviewer:
        raise CloudError(f"an independent review of {author}'s work runs on {reviewer}, not {engine}: "
                         "drop --engine, or pass --engine " + reviewer)
    if repo and str(reviewed.get("repo", "")).lower() != repo.lower():
        raise CloudError(f"job {str(reviewed['job_id'])[:8]} is for {reviewed.get('repo')}, but --cwd is {repo}")
    branch = reviewed.get("landed_branch") or (reviewed.get("result_branch") if reviewed.get("status") == "done" else None)
    head = reviewed.get("pushed_sha") or reviewed.get("result_commit")
    if not branch or not head:
        raise CloudError(f"job {str(reviewed['job_id'])[:8]} has no pushed branch to review yet: land it first "
                         "(`lazytools-code-bridge apply <job> --land`, or submit with --land and run `cloud-sync`)")
    if root is not None:
        tip = _cloud._remote_base(root, str(branch))
        if tip and tip != head:
            raise CloudError(f"{branch} has advanced to {tip[:12]} since job {str(reviewed['job_id'])[:8]} pushed "
                             f"{str(head)[:12]}: a review would be recorded against a commit the reviewer did not "
                             "inspect. Land or submit the new work as a job and review that, or re-run once the "
                             "branch is back at the reviewed commit")
    base = str(reviewed.get("pr_base") or reviewed.get("branch") or "")
    task = review_brief(branch=str(branch), head=str(head), base=base, author=author, job_id=job_id,
                        reviewer=reviewer, extra=extra)
    review = {
        "of": str(reviewed["job_id"]), "author": author, "reviewer": reviewer, "branch": str(branch),
        "head": str(head), "base": base, "pr_number": reviewed.get("pr_number"), "pr_url": reviewed.get("pr_url"),
        "file": REVIEW_FILE,
    }
    return {"engine": reviewer, "branch": str(branch), "task": task, "review": review}


_VERDICT_RE = re.compile(r"(?im)^[\s>*_`-]*verdict\s*:\s*[*_`]*\s*(no[- ]?blocking|non[- ]?blocking|blocking)\b")
_VERDICT_LINE_RE = re.compile(r"(?im)^[\s>*_`-]*verdict\s*:.*(?:\n|$)")


def parse_verdict(text: str) -> tuple[str | None, str]:
    """``(verdict, body without verdict lines)``; verdict is ``blocking``/``no-blocking`` or ``None``."""
    matches = _VERDICT_RE.findall(text)
    verdict = None
    if matches:
        verdict = "no-blocking" if matches[-1].lower().startswith(("no", "non")) else "blocking"
    return verdict, _VERDICT_LINE_RE.sub("", text).strip()


def added_lines(diff: str, filename: str) -> str:
    """The lines a unified ``diff`` adds to ``filename`` (a review written as a new file)."""
    out: list[str] = []
    current = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = match.group(2) if match else None
        elif current == filename and line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return "\n".join(out)


def review_text(job: dict[str, Any]) -> str:
    """The reviewer's REVIEW.md from the provider ('' when it cannot be read)."""
    prov = _cloud.provider(str(job["engine"]))
    try:
        job_file = getattr(prov, "job_file", None)
        if job_file is not None:
            return job_file(job, REVIEW_FILE)
        return added_lines(prov.diff(str(job["cloud_handle"])), REVIEW_FILE)
    except CloudError:
        return ""


def build_review_comment(*, author: str, reviewer: str, verdict: str, head: str, body: str) -> str:
    """The ONE PR comment: marker line, four header lines, then the findings. No footer."""
    body = body.strip()
    if len(body) > _MAX_COMMENT_BODY:
        body = body[:_MAX_COMMENT_BODY].rstrip() + "\n\n[review truncated]"
    head_block = f"{REVIEW_MARKER}\nauthor: {author}\nreviewer: {reviewer}\nverdict: {verdict}\nhead: {head}"
    return redact(f"{head_block}\n\n{body}" if body else head_block)


def post_review(store: Any, registry: Any, job: dict[str, Any]) -> dict[str, Any]:
    """Post a finished review job's verdict to the reviewed job's PR (once). Idempotent."""
    job_id = str(job["job_id"])
    job = _cloud._merged(store, registry, job_id)
    review = job.get("review")
    if not isinstance(review, dict):
        raise CloudError(f"job {job_id[:8]} is not a review job")
    if job.get("review_posted_at") or job.get("review_comment_url"):
        return {"job_id": job_id, "posted": True, "already": True, "url": job.get("review_comment_url"),
                "verdict": job.get("review_verdict")}
    if job.get("status") != "done":
        raise CloudError(f"review job {job_id[:8]} is {job.get('status')}, not done")
    if not _claim(store, job_id, "review"):
        raise CloudError(f"review {job_id[:8]} is being posted by another process right now; retry in a minute")
    try:
        text = review_text(job)
        verdict, body = parse_verdict(text) if text else (None, "")
        if not text:
            message = _cloud.redact(str(job.get("result") or job.get("cloud_message") or ""))
            verdict, body = parse_verdict(message)
            if verdict is None:
                attempts = int(job.get("review_attempts") or 0) + 1
                note = f"the reviewer left no readable {REVIEW_FILE} (attempt {attempts} of {MAX_REVIEW_ATTEMPTS})"
                _update_meta(store, job_id, {"review_attempts": attempts, "review_note": note})
                raise CloudError(note)
            text = message
        if verdict is None:
            verdict = "blocking"
            body = f"(no `verdict:` line in {REVIEW_FILE}: treated as blocking)\n\n{body}"
        _update_meta(store, job_id, {"review_verdict": verdict, "review_summary": redact(body)[:500]})
        pr_number = review.get("pr_number")
        if not pr_number:
            note = "the reviewed job has no PR: the review is in this result only"
            _update_meta(store, job_id, {"review_note": note, "review_posted_at": time.time()})
            return {"job_id": job_id, "posted": False, "verdict": verdict, "note": note}
        comment = build_review_comment(author=str(review["author"]), reviewer=str(review["reviewer"]),
                                       verdict=verdict, head=str(review["head"]), body=body)
        repo = str(job["repo"])
        wanted = (f"reviewer: {review['reviewer']}", f"head: {review['head']}")
        for existing in _gh.pr_comments(repo, int(pr_number)):
            lines = existing.splitlines()
            if lines[:1] == [REVIEW_MARKER] and all(w in lines for w in wanted):
                _update_meta(store, job_id, {"review_posted_at": time.time(), "review_note": None})
                return {"job_id": job_id, "posted": True, "already": True, "verdict": verdict, "url": None}
        url = _gh.pr_comment(repo, int(pr_number), comment)
        _update_meta(store, job_id, {"review_posted_at": time.time(), "review_comment_url": url or None,
                                     "review_note": None})
        return {"job_id": job_id, "posted": True, "already": False, "verdict": verdict, "url": url or None}
    finally:
        _release(store, job_id, "review")


# --------------------------------------------------------------------------- #
# reconcile: what `wait` and `cloud-sync` do with a job
# --------------------------------------------------------------------------- #


def needs_finishing(job: dict[str, Any]) -> bool:
    """A done job with landing or a review comment still owed."""
    if job.get("status") != "done":
        return False
    if isinstance(job.get("review"), dict):
        return not (job.get("review_posted_at") or job.get("review_comment_url")) and \
            int(job.get("review_attempts") or 0) < MAX_REVIEW_ATTEMPTS
    return bool(job.get("land")) and not job.get("landed_at")


def process_job(store: Any, registry: Any, job: dict[str, Any], *, db_path: Path | None = None) -> dict[str, Any]:
    """Refresh one cloud job, then land it / post its review / record its failure. Returns a report row.

    Never raises for a provider or git problem: the row's ``action`` says what
    happened (``landed``, ``review-posted``, ``land-failed``, ``review-failed``,
    ``failed``, ``waiting``, ``none``) and ``detail`` why.
    """
    job_id = str(job["job_id"])
    job = _cloud.refresh(store, registry, _cloud._merged(store, registry, job_id))
    row: dict[str, Any] = {"job_id": job_id, "engine": job.get("engine"), "status": job.get("status"),
                           "action": "none", "detail": ""}
    status = job.get("status")
    if status in _cloud.ACTIVE_STATUSES:
        row.update(action="waiting", detail=str(job.get("cloud_status") or job.get("last_poll_error") or ""))
    elif status in ("failed", "interrupted"):
        reason = redact(str(job.get("error") or f"cloud task {status}"))
        row.update(action="failed", detail=reason)
        if not job.get("failure_reported_at"):
            _update_meta(store, job_id, {"failure_reported_at": time.time(), "failure_reason": reason})
        else:
            row["action"] = "none"
    elif needs_finishing(job):
        try:
            if isinstance(job.get("review"), dict):
                outcome = post_review(store, registry, job)
                row.update(action="review-posted" if outcome["posted"] else "review-noted",
                           detail=str(outcome.get("url") or outcome.get("note") or f"verdict {outcome['verdict']}"))
            else:
                outcome = land_job(store, registry, job, db_path=db_path)
                row.update(action="landed", detail=str(outcome.get("pr_url") or outcome.get("note") or outcome["branch"]))
        except CloudError as exc:
            kind = "review-failed" if isinstance(job.get("review"), dict) else "land-failed"
            row.update(action=kind, detail=redact(str(exc)))
    return row


def sync(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Reconcile every live cloud job (and every finished one still owed a landing or comment)."""
    store = _store.build_store(db_path)
    registry = _store.build_job_registry(store)
    rows = []
    for record in sorted(_store.all_job_records(store), key=lambda r: r.get("created_at") or 0):
        if record.get("where") != _cloud.WHERE_CLOUD:
            continue
        job = _cloud._merged(store, registry, str(record["job_id"]))
        owed = job.get("status") in _cloud.ACTIVE_STATUSES or needs_finishing(job) or (
            job.get("status") in ("failed", "interrupted") and not job.get("failure_reported_at"))
        if owed:
            rows.append(process_job(store, registry, job, db_path=db_path))
    return rows


__all__ = [
    "REVIEW_MARKER",
    "build_review_comment",
    "commit_message",
    "job_title",
    "land_job",
    "needs_finishing",
    "opposite_engine",
    "parse_verdict",
    "post_review",
    "prepare_review",
    "process_job",
    "sync",
]
