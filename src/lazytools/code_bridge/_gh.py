"""The GitHub CLI (``gh``) calls cloud landing and cloud reviews need.

Everything goes through :func:`run_gh`: no shell, a timeout, the body of a PR
or comment on stdin (never on the command line), and credential-looking text
redacted from any error. ``GH_BIN`` overrides the executable (tests point it
at a fake). Authentication is ``gh``'s own (``gh auth login`` or ``GH_TOKEN``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

from lazytools.code_bridge._cloud import CloudError, redact

GH_TIMEOUT = 120.0
_PR_URL_RE = re.compile(r"https?://[^\s/]+/[^\s/]+/[^\s/]+/pull/(\d+)")


def gh_argv() -> list[str]:
    found = os.environ.get("GH_BIN") or shutil.which("gh")
    if not found:
        raise CloudError(
            "GitHub CLI (gh) not found: install it and run `gh auth login` (or set GH_BIN); landing a cloud "
            "job and posting a review need it"
        )
    return [found]


def run_gh(*args: str, stdin: str | None = None) -> str:
    try:
        proc = subprocess.run(
            [*gh_argv(), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT,
            input=stdin if stdin is not None else None,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise CloudError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} timed out after {GH_TIMEOUT:g}s") from exc
    except OSError as exc:
        raise CloudError(f"could not start the GitHub CLI: {exc}") from exc
    if proc.returncode != 0:
        detail = redact((proc.stderr or proc.stdout or "").strip())[:2000]
        raise CloudError(f"gh {' '.join(args[:2])} exited {proc.returncode}: {detail}")
    return proc.stdout


def _json(*args: str) -> Any:
    out = run_gh(*args)
    try:
        return json.loads(out)
    except ValueError as exc:
        raise CloudError(f"gh {' '.join(args[:2])} printed no JSON: {redact(out.strip())[:500]}") from exc


def pr_number_from_url(url: str) -> int | None:
    match = _PR_URL_RE.search(url or "")
    return int(match.group(1)) if match else None


def default_branch(repo: str) -> str:
    payload = _json("repo", "view", repo, "--json", "defaultBranchRef")
    ref = payload.get("defaultBranchRef") if isinstance(payload, dict) else None
    name = ref.get("name") if isinstance(ref, dict) else None
    if not isinstance(name, str) or not name:
        raise CloudError("gh repo view returned no default branch")
    return name


def pr_view(repo: str, number: int, *, publication: bool = False, timing: bool = False) -> dict[str, Any]:
    """``number, url, state, title, headRefName, headRefOid, baseRefName, isCrossRepository`` of one PR."""
    fields = "number,url,state,title,headRefName,headRefOid,baseRefName,isCrossRepository"
    if publication:
        fields += ",isDraft,baseRefOid"
    if timing:
        fields += ",createdAt"
    payload = _json("pr", "view", str(number), "--repo", repo, "--json", fields)
    if not isinstance(payload, dict):
        raise CloudError(f"gh pr view {number} returned an unexpected payload")
    return payload


def pr_for_branch(repo: str, branch: str) -> dict[str, Any] | None:
    """The open PR whose head is ``branch``, if any."""
    payload = _json(
        "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,url,headRefOid,baseRefName"
    )
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return None


def publication_prs(repository: str, branch: str) -> list[dict[str, Any]]:
    """PRs, newest first, whose head is in the destination repository."""
    owner = repository.split("/", 1)[0]
    pages = _json(
        "api", "--hostname", "github.com", "--method", "GET", "--paginate", "--slurp",
        f"repos/{repository}/pulls", "-f", f"head={owner}:{branch}", "-f", "state=all",
    )
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise CloudError("gh api returned an unexpected pull request list")
    matches = []
    for page in pages:
        for pr in page:
            head = pr["head"]
            source = head["repo"]
            if head["ref"] == branch and source and source["full_name"].casefold() == repository:
                matches.append(pr)
    return matches


def publication_ci(repository: str, number: int, sha: str) -> tuple[str, str | None, int]:
    """Read the PR head and the check rollup for an immutable commit in one query."""
    query = """
    query($owner: String!, $name: String!, $number: Int!, $sha: GitObjectID!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) { headRefOid }
        object(oid: $sha) {
          ... on Commit {
            oid
            statusCheckRollup { state contexts(first: 1) { totalCount } }
          }
        }
      }
    }
    """
    owner, name = repository.split("/", 1)
    payload = _json("api", "graphql", "--hostname", "github.com", "-f", f"query={query}",
                    "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}", "-f", f"sha={sha}")
    try:
        repo = payload["data"]["repository"]
        head = repo["pullRequest"]["headRefOid"]
        if not isinstance(head, str):
            raise TypeError
        if head != sha:
            return head, None, 0
        commit = repo["object"]
        if commit["oid"] != sha:
            raise ValueError
        rollup = commit["statusCheckRollup"]
        if rollup is None:
            return head, None, 0
        count, state = rollup["contexts"]["totalCount"], rollup["state"]
        if type(count) is not int or count < 0 or state not in {"SUCCESS", "FAILURE", "ERROR", "PENDING", "EXPECTED"}:
            raise ValueError
        return head, state, count
    except (KeyError, TypeError, ValueError) as exc:
        raise CloudError("GitHub returned an unexpected CI result for the published commit") from exc


def pr_create(repo: str, *, base: str, head: str, title: str, body: str, draft: bool = False) -> str:
    """Open a PR; returns its URL."""
    out = run_gh(
        "pr",
        "create",
        "--repo",
        repo,
        "--base",
        base,
        "--head",
        head,
        "--title",
        title,
        "--body-file",
        "-",
        *(("--draft",) if draft else ()),
        stdin=body,
    )
    match = _PR_URL_RE.search(out)
    if not match:
        raise CloudError(f"gh pr create printed no PR URL: {redact(out.strip())[:500]}")
    return match.group(0)


def pr_comments(repo: str, number: int) -> list[str]:
    payload = _json("pr", "view", str(number), "--repo", repo, "--json", "comments")
    comments = payload.get("comments") if isinstance(payload, dict) else None
    return [str(c.get("body") or "") for c in comments or [] if isinstance(c, dict)]


def pr_comment(repo: str, number: int, body: str) -> str:
    """Post ONE comment; returns the comment URL when gh prints it, else ``""``."""
    return run_gh("pr", "comment", str(number), "--repo", repo, "--body-file", "-", stdin=body).strip()


__all__ = [
    "default_branch",
    "gh_argv",
    "pr_comment",
    "pr_comments",
    "pr_create",
    "pr_for_branch",
    "pr_number_from_url",
    "pr_view",
    "run_gh",
]
