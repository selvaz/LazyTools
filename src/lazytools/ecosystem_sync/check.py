"""Read-only evidence collection. All mutations are text in a realignment plan."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CLASSES = ("auto_safe", "session", "owner")
PROTECTED = "lazytools-mcp-main"
DEFAULT_ROOT = Path(r"C:\Users\Administrator\Documents\GitHub")


def run(argv, *, timeout=60):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env, check=False
    )


def command(*args):
    """PowerShell command with literal arguments; never evaluated by the check."""
    return "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in args)


def manifest_refs(path):
    verified = tomllib.loads(path.read_text(encoding="utf-8"))["verified"]
    refs = {
        entry["repo"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"): entry["ref"]
        for entry in verified["github"].values()
    }
    refs["LazyBridge"] = "v" + verified["lazybridge"]
    return refs


def repository_name(remote):
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)selvaz/([^/]+?)(?:\.git)?/?", remote or "")
    return match[1] if match else None


def age_days(value, now):
    return max(0, (now - datetime.fromisoformat(value.replace("Z", "+00:00"))).total_seconds() / 86400)


class Checker:
    def __init__(
        self, root, manifest, *, fetch=True, stale_days=7, git_runner=run, gh_runner=run, audit_runner=run, now=None
    ):
        self.root, self.manifest = Path(root), Path(manifest)
        self.fetch, self.stale_days = fetch, stale_days
        self.git_runner, self.gh_runner, self.audit_runner = git_runner, gh_runner, audit_runner
        self.now = now or datetime.now(UTC)
        self.refs = {repo.casefold(): ref for repo, ref in manifest_refs(self.manifest).items()}
        self.actions, self.observations, self.errors = [], [], []
        self.repositories, self.worktrees, self.pull_requests = [], [], []
        self.pr_cache = {}
        self.github_seen = set()
        self.remote_candidates = set()
        self.audit = {}

    def action(self, cls, repo, fact, proposed, risk):
        self.actions.append({"class": cls, "repo": repo, "fact": fact, "command": proposed, "risk": risk})

    def error(self, repo, operation, exc):
        detail = f"{operation}: {exc}"
        self.errors.append({"repo": repo, "error": detail})
        self.action(
            "session",
            repo,
            "Check incomplete: " + detail,
            command("lazytools-ecosystem-sync", "check", "--root", self.root, "--no-fetch"),
            "Unknown state; retry the failed read before realignment",
        )

    def git(self, path, *args, allowed=(0,)):
        if Path(path).name.lower() == PROTECTED:
            raise ValueError("protected production editable checkout")
        result = self.git_runner(
            [
                "git",
                "--no-optional-locks",
                "-c",
                f"safe.directory={Path(path).resolve().as_posix()}",
                "-C",
                str(path),
                *args,
            ],
            timeout=60,
        )
        if result.returncode not in allowed:
            raise RuntimeError(f"git {' '.join(args)} exited {result.returncode}: {result.stderr.strip()[:350]}")
        return result.stdout.rstrip("\r\n")

    def gh(self, *args, allowed=(0,)):
        result = self.gh_runner(["gh", *map(str, args)], timeout=60)
        if result.returncode not in allowed:
            raise RuntimeError(
                f"gh {' '.join(map(str, args))} exited {result.returncode}: {result.stderr.strip()[:350]}"
            )
        return json.loads(result.stdout or "[]")

    def attempt(self, repo, operation, fn, default=None):
        try:
            return fn()
        except Exception as exc:
            self.error(repo, operation, f"{type(exc).__name__}: {exc}")
            return default

    def status(self, path):
        # -z preserves spaces, quotes, newlines and rename pairs.
        raw = self.git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries, filenames = [], []
        tokens = iter(raw.split("\0"))
        for token in tokens:
            if not token:
                continue
            entries.append(token[:2])
            filenames.append(token[3:])
            if "R" in token[:2] or "C" in token[:2]:
                next(tokens, None)
        return {"dirty": sum(x != "??" for x in entries), "untracked": entries.count("??")}, filenames

    def dirty_work(self, path, repo, counts, files):
        mtimes = []
        for name in files:
            file = path / name
            try:
                if file.exists() and not file.is_symlink():
                    mtimes.append(file.stat().st_mtime)
            except OSError:
                pass
        oldest = max(0, (self.now.timestamp() - min(mtimes)) / 86400) if mtimes else None
        self.action(
            "session",
            repo,
            f"{path}: dirty={counts['dirty']}, untracked={counts['untracked']}; "
            + (f"oldest file mtime {oldest:.1f} days" if oldest is not None else "change age unknown")
            + (f" (STALE >= {self.stale_days} days)" if oldest is not None and oldest >= self.stale_days else ""),
            command("git", "-C", path, "status", "--short"),
            "Preserve uncommitted work; mtime is only an age proxy",
        )
        return round(oldest, 2) if oldest is not None else None

    def inspect_checkout(self, path, repo, default):
        branch = self.git(path, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1)) or "DETACHED"
        head = self.git(path, "rev-parse", "--verify", "HEAD", allowed=(0, 128)) or None
        counts, files = self.status(path)
        ahead, behind = (
            map(
                int,
                self.git(path, "rev-list", "--left-right", "--count", f"HEAD...refs/remotes/origin/{default}").split(),
            )
            if default and head
            else (None, None)
        )
        ref = self.refs.get(repo.casefold())
        pin = self.git(path, "rev-parse", "--verify", f"{ref}^{{commit}}", allowed=(0, 128)) if ref else None
        expected = bool(ref and ref.startswith("v") and head and pin == head)
        row = {
            "repo": repo,
            "path": str(path),
            "branch": branch,
            "head": head,
            "default_branch": default,
            "ahead": ahead,
            "behind": behind,
            **counts,
            "manifest_ref": ref,
            "pinned_expected": expected,
        }
        self.repositories.append(row)
        if expected:
            self.observations.append(
                {"repo": repo, "fact": f"{path}: at manifest {ref}; ahead={ahead}, behind={behind} EXPECTED"}
            )
        elif ahead or behind:
            cls = "owner" if repo.lower() == "lazybricks" or "runtime" in path.name.lower() else "session"
            self.action(
                cls,
                repo,
                f"{path}: {branch}, ahead={ahead}, behind={behind} origin/{default}",
                command("git", "-C", path, "log", "--oneline", "--left-right", f"HEAD...origin/{default}"),
                "Owner decision: deliberately behind"
                if cls == "owner"
                else "Review divergence and editable-install consumers before moving HEAD",
            )
        if counts["dirty"] or counts["untracked"]:
            row["oldest_change_days"] = self.dirty_work(path, repo, counts, files)

    def unpushed(self, path, repo):
        for branch in self.git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
            count = int(self.git(path, "rev-list", "--count", branch, "--not", "--remotes"))
            if count:
                self.action(
                    "session",
                    repo,
                    f"Local branch {branch}: {count} commits absent from every remote branch",
                    command("git", "-C", path, "log", "--oneline", branch, "--not", "--remotes"),
                    "Inspect commits and coordinate with the owning session before publishing",
                )

    def prs(self, repo, state, head=None):
        key = (repo.casefold(), state, head)
        if key not in self.pr_cache:
            args = [
                "pr",
                "list",
                "--repo",
                f"selvaz/{repo}",
                "--state",
                state,
                "--limit",
                "1000",
                "--json",
                "number,title,state,createdAt,updatedAt,mergedAt,headRefName,headRefOid,isCrossRepository,url",
            ]
            if head:
                args += ["--head", head]
            rows = self.gh(*args)
            if len(rows) == 1000:
                raise RuntimeError("PR result limit reached; evidence incomplete")
            self.pr_cache[key] = rows
        return self.pr_cache[key]

    def inspect_worktrees(self, path, repo, github):
        raw = self.git(path, "worktree", "list", "--porcelain", "-z")
        records: list[dict[str, Any]] = []
        record: dict[str, Any] = {}
        for token in raw.split("\0"):
            if token.startswith("worktree "):
                if record:
                    records.append(record)
                record = {"path": token[9:]}
            elif token.startswith("branch "):
                record["branch"] = token[7:].removeprefix("refs/heads/")
            elif token.startswith("HEAD "):
                record["head"] = token[5:]
            elif token == "locked" or token.startswith("locked "):
                record["locked"] = True
            elif token == "prunable" or token.startswith("prunable "):
                record["prunable"] = True
        if record:
            records.append(record)
        for record in records:
            record["repo"] = repo
            self.worktrees.append(record)
            target = Path(record["path"])
            if target.name.lower() == PROTECTED:
                record["status"] = "protected: not inspected"
                continue
            if not target.exists() and record.get("prunable"):
                record["status"] = "missing; prunable registration"
                record["pr_state"] = "unknown"
                if github and record.get("branch"):
                    prs = self.attempt(
                        repo,
                        f"stale worktree PR {record['branch']}",
                        lambda b=record["branch"]: self.prs(repo, "all", b),
                        [],
                    )
                    latest = max(prs or [], key=lambda x: x["number"], default=None)
                    record["pr_state"] = latest["state"] if latest else "none"
                self.action(
                    "auto_safe",
                    repo,
                    f"Missing worktree {target}: stale prunable registration",
                    command("git", "-C", path, "worktree", "prune", "--expire", "now"),
                    "Revalidate with worktree prune --dry-run; removes all eligible stale registrations, not files",
                )
                continue
            status = self.attempt(repo, f"worktree status {target}", lambda t=target: self.status(t))
            if status is None:
                continue
            counts, files = status
            record.update(counts)
            if target.parent != self.root and (counts["dirty"] or counts["untracked"]):
                record["oldest_change_days"] = self.dirty_work(target, repo, counts, files)
            branch = record.get("branch")
            if not github or not branch:
                record["pr_state"] = "detached" if not branch else "unknown (non-GitHub origin)"
                continue
            prs = self.attempt(repo, f"worktree PR {branch}", lambda b=branch: self.prs(repo, "all", b))
            if prs is None:
                record["pr_state"] = "unknown"
                continue
            prs = [pr for pr in prs if not pr.get("isCrossRepository")]
            latest = max(prs, key=lambda x: x["number"], default=None)
            record["pr_state"] = latest["state"] if latest else "none"
            record["pr_number"] = latest["number"] if latest else None
            # Never nominate canonical checkouts, runtimes or locked worktrees for removal.
            if latest and latest["state"] in ("MERGED", "CLOSED") and target != path:
                dirty = counts["dirty"] + counts["untracked"]
                runtime = "runtime" in target.name.lower()
                merged = latest["state"] == "MERGED"
                cls = (
                    "owner"
                    if runtime
                    else ("auto_safe" if merged and not dirty and not record.get("locked") else "session")
                )
                proposed = (
                    command("git", "-C", path, "worktree", "remove", "--", target)
                    if cls == "auto_safe"
                    else command("git", "-C", target, "status", "--short")
                )
                self.action(
                    cls,
                    repo,
                    f"Worktree {target}, {branch}, PR #{latest['number']} {latest['state']}, "
                    f"dirty={counts['dirty']}, untracked={counts['untracked']}",
                    proposed,
                    "Recheck PR, cleanliness and active sessions; never use --force"
                    if cls == "auto_safe"
                    else "Preserve work; runtime pins require owner deployment approval"
                    if runtime
                    else "Owning session must reconcile remaining work",
                )

    def inspect_pr(self, repo, pr):
        number = pr["number"]
        checks = self.gh(
            "pr", "checks", number, "--repo", f"selvaz/{repo}", "--json", "name,state,bucket", allowed=(0, 1, 8)
        )
        # --slurp makes pagination one valid JSON array, including an empty page.
        comments: list[Any] = []
        for endpoint in (f"issues/{number}/comments", f"pulls/{number}/reviews", f"pulls/{number}/comments"):
            pages = self.gh("api", f"repos/selvaz/{repo}/{endpoint}", "--paginate", "--slurp")
            comments.extend(item for page in pages for item in page)
        summaries = [
            c
            for c in comments
            if c.get("user", {}).get("login", "").removesuffix("[bot]") == "chatgpt-codex-connector"
            and "has_unresolved_findings" in (c.get("body") or "")
        ]
        summary = max(
            summaries,
            key=lambda c: c.get("updated_at") or c.get("submitted_at") or c.get("created_at", ""),
            default=None,
        )
        body = summary.get("body", "") if summary else ""
        flag = re.search(r'has_unresolved_findings[\s"\x27]*[:=]\s*(true|false)', body, re.I)
        unresolved = flag[1].lower() == "true" if flag else None
        reviewed = bool(
            summary
            and (
                summary.get("commit_id") == pr["headRefOid"]
                or re.search(r"(?<![0-9a-f])" + re.escape(pr["headRefOid"]) + r"(?![0-9a-f])", body)
            )
        )
        green = bool(checks) and all(c["bucket"] == "pass" for c in checks)
        ready = green and reviewed and unresolved is False and not pr.get("isCrossRepository")
        row = {
            **pr,
            "repo": repo,
            "age_days": round(age_days(pr["createdAt"], self.now), 2),
            "checks": checks,
            "codex_summary": body or None,
            "has_unresolved_findings": unresolved,
            "head_reviewed": reviewed,
        }
        self.pull_requests.append(row)
        fact = (
            f"PR #{number} ({row['age_days']:.1f} days): {pr['title']}; "
            f"CI={'pass' if green else 'pending/failing/absent'}, Codex unresolved={unresolved}, head reviewed={reviewed}; "
        )
        if ready:
            task = "ready to merge via safe_merge.sh (read comments first)"
            proposed = command("bash", r"C:\ProgramData\lazytools\safe_merge.sh", repo, number)
        else:
            task = "fix findings" if unresolved else "resolve CI and obtain Codex review of current head"
            proposed = command("gh", "pr", "view", number, "--repo", f"selvaz/{repo}", "--comments")
        self.action(
            "session",
            repo,
            fact + task,
            proposed,
            "safe_merge.sh requires reading all comments and a current --ack count before merge",
        )

    def remote_merged(self, path, repo, default, open_prs):
        refs = dict(
            line.split(" ", 1)
            for line in self.git(
                path, "for-each-ref", "--format=%(refname:strip=3) %(objectname)", "refs/remotes/origin"
            ).splitlines()
        )
        active = {pr["headRefName"] for pr in open_prs}
        for pr in self.prs(repo, "merged"):
            branch = pr["headRefName"]
            if (
                branch in refs
                and branch not in {default, "HEAD"}
                and branch not in active
                and not pr.get("isCrossRepository")
                and refs[branch] == pr["headRefOid"]
            ):
                identity = (repo.casefold(), branch, refs[branch])
                if identity in self.remote_candidates:
                    continue
                self.remote_candidates.add(identity)
                self.action(
                    "auto_safe",
                    repo,
                    f"Remote branch {branch} still exists at merged PR #{pr['number']} head",
                    command(
                        "git",
                        "-C",
                        path,
                        "push",
                        f"--force-with-lease=refs/heads/{branch}:{refs[branch]}",
                        "origin",
                        "--delete",
                        "--",
                        branch,
                    ),
                    "Deletion candidate only; verify no active session; lease protects against newer remote commits",
                )

    def releases(self, path, repo):
        ref = self.refs.get(repo.casefold())
        if not ref or not ref.startswith("v"):
            return
        tags = self.git(path, "tag", "--list", "v*", "--sort=-version:refname").splitlines()
        latest = tags[0] if tags else None
        self.observations.append({"repo": repo, "fact": f"Latest locally known release {latest}; manifest {ref}"})
        if latest and latest != ref:
            # A rollback pin may be deliberate; the plan never promotes it automatically.
            self.action(
                "session",
                repo,
                f"Tagged but not deployed to manifest: latest={latest}, manifest={ref}",
                command("git", "-C", path, "log", "--oneline", f"{ref}..{latest}"),
                "Review release order and manifest promotion; production deployment requires owner approval",
            )

    def inspect_repo(self, path, repo, github, *, has_origin=True):
        if self.fetch and has_origin:
            self.attempt(repo, "fetch", lambda: self.git(path, "fetch", "--prune", "origin"))
        default_ref = self.git(path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", allowed=(0, 1))
        default = default_ref.removeprefix("refs/remotes/origin/") if default_ref else None
        if github:
            metadata = self.attempt(
                repo, "default branch", lambda: self.gh("repo", "view", f"selvaz/{repo}", "--json", "defaultBranchRef")
            )
            if metadata:
                default = metadata["defaultBranchRef"]["name"]
        if not default and has_origin:
            self.error(
                repo, "default branch", "origin default branch unavailable (set origin/HEAD or restore GitHub access)"
            )
        self.attempt(repo, "checkout", lambda: self.inspect_checkout(path, repo, default))
        self.attempt(repo, "unpushed branches", lambda: self.unpushed(path, repo))
        self.attempt(repo, "worktrees", lambda: self.inspect_worktrees(path, repo, github))
        self.attempt(repo, "release tags", lambda: self.releases(path, repo))
        if github:
            prs = self.attempt(repo, "open PRs", lambda: self.prs(repo, "open"))
            if prs is not None:
                if repo.casefold() not in self.github_seen:
                    self.github_seen.add(repo.casefold())
                    for pr in prs:
                        self.attempt(repo, f"PR #{pr['number']} review/CI", lambda p=pr: self.inspect_pr(repo, p))
                self.attempt(repo, "merged remote branches", lambda: self.remote_merged(path, repo, default, prs))

    def production_audit(self):
        script = self.root / "investmentcommittee" / "scripts" / "ecosystem_audit.py"
        if not script.exists():
            self.observations.append({"repo": "ecosystem", "fact": "Complementary production audit not present"})
            return
        result = self.audit_runner(
            [sys.executable, "-B", str(Path(__file__).with_name("audit.py")), str(self.root), str(self.manifest)],
            timeout=300,
        )
        if result.returncode:
            raise RuntimeError(f"production audit exited {result.returncode}: {result.stderr.strip()[:350]}")
        self.audit = json.loads(result.stdout)
        for problem in self.audit["problems"]:
            text = problem if isinstance(problem, str) else json.dumps(problem, sort_keys=True)
            if "cannot " in text:
                self.error("production", "audit evidence", text)
            else:
                cls = "session" if text.startswith("runtime ") and "release set says" in text else "owner"
                self.action(
                    cls,
                    "production",
                    text,
                    command(sys.executable, "-B", script),
                    "Production env/runtime/wiring realignment requires owner approval and an idle window",
                )
        self.observations.append(
            {
                "repo": "production",
                "fact": f"ecosystem_audit.drift(): {len(self.audit['problems'])} drift(s); "
                f"{len(self.audit['runtimes'])} runtime pins inspected",
            }
        )
        for runtime in self.audit["runtimes"]:
            self.observations.append(
                {"repo": "production", "fact": f"Runtime {runtime['path']}: {runtime['tag'] or 'untagged'}"}
            )

    def disk(self):
        for drive in (Path("C:/"), Path("D:/")):
            if drive.exists():
                free = shutil.disk_usage(drive).free / 1024**3
                self.observations.append({"repo": "disk", "fact": f"{drive}: {free:.2f} GB free"})
                if free < 10:
                    self.action(
                        "owner",
                        "disk",
                        f"{drive}: low free disk space ({free:.2f} GB)",
                        command("Get-PSDrive", "-PSProvider", "FileSystem"),
                        "Owner selects files to preserve",
                    )
        candidates = list((self.root / "_wt").glob("*/.tmp"))
        pytest_tmp = Path("D:/pytest-tmp")
        if pytest_tmp.exists():
            candidates.extend(p for p in pytest_tmp.iterdir() if p.is_dir() and not p.is_symlink())
        for path in sorted(candidates):
            if path.is_symlink():
                continue
            size, newest = 0, path.stat().st_mtime
            for base, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
                newest = max(newest, Path(base).stat().st_mtime)
                for name in files:
                    file = Path(base) / name
                    if not file.is_symlink():
                        stat = file.stat()
                        size += stat.st_size
                        newest = max(newest, stat.st_mtime)
            age = (self.now.timestamp() - newest) / 86400
            fact = f"{path}: {size / 1024**2:.2f} MB temporary files; newest mtime {age:.1f} days"
            self.observations.append({"repo": "disk", "fact": fact})
            if size and age >= self.stale_days:
                self.action(
                    "auto_safe",
                    "disk",
                    fact,
                    command("Remove-Item", "-LiteralPath", path, "-Recurse"),
                    "Cleanup candidate: verify resolved path, no active process and no needed artifacts before deleting",
                )

    def check(self):
        # A git file marks a linked worktree; scan it via its owning primary checkout
        # when available, so fetch/branch/PR collection happens only once per repo.
        paths = sorted(
            p for p in self.root.iterdir() if p.is_dir() and (p / ".git").exists() and p.name.lower() != PROTECTED
        )
        groups: dict[str, list[Path]] = {}
        for path in paths:
            common = self.attempt(
                path.name,
                "repository discovery",
                lambda p=path: self.git(p, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            )
            if common:
                groups.setdefault(common.casefold(), []).append(path)
        for siblings in groups.values():
            path = min(siblings, key=lambda p: (not (p / ".git").is_dir(), "runtime" in p.name.lower(), str(p)))
            # `git remote get-url origin` exits 2 when there is no origin: a
            # local-only repository is a fact for the owner, not a check failure.
            remote = self.attempt(
                path.name, "origin URL", lambda p=path: self.git(p, "remote", "get-url", "origin", allowed=(0, 2))
            )
            if not remote:
                self.action(
                    "owner",
                    path.name,
                    f"{path}: no origin remote (local-only repository; nothing is backed up on GitHub)",
                    command("git", "-C", path, "remote", "-v"),
                    "Decide whether to publish it or record that it stays local on purpose.",
                )
            repo = repository_name(remote)
            self.attempt(
                repo or path.name,
                "repository",
                lambda p=path, r=repo, origin=bool(remote): self.inspect_repo(
                    p, r or p.name, bool(r), has_origin=origin
                ),
            )
            # Every direct linked checkout still gets its own HEAD/divergence/age evidence.
            default = next((r["default_branch"] for r in self.repositories if r["path"] == str(path)), None)
            for sibling in siblings:
                if sibling != path:
                    self.attempt(
                        repo or path.name,
                        f"checkout {sibling}",
                        lambda p=sibling, r=repo or path.name, d=default: self.inspect_checkout(p, r, d),
                    )
        self.attempt("production", "complementary audit", self.production_audit)
        self.attempt("disk", "disk inspection", self.disk)
        self.actions.sort(key=lambda a: (CLASSES.index(a["class"]), a["repo"].casefold(), a["fact"]))
        return {
            "schema_version": 1,
            "generated_at": self.now.isoformat(),
            "root": str(self.root),
            "manifest": str(self.manifest),
            "fetch": self.fetch,
            "stale_days": self.stale_days,
            "actions": self.actions,
            "observations": self.observations,
            "errors": self.errors,
            "repositories": self.repositories,
            "worktrees": self.worktrees,
            "pull_requests": self.pull_requests,
            "production_audit": self.audit,
        }


def exit_code(plan):
    return 1 if plan["errors"] else 2 if plan["actions"] else 0


def markdown(plan):
    counts = {cls: sum(a["class"] == cls for a in plan["actions"]) for cls in CLASSES}
    lines = [
        f"# Ecosystem realignment plan — {plan['generated_at'][:10]}",
        f"Root: {plan['root']}",
        f"Manifest: {plan['manifest']}",
        f"Fetch: {'enabled' if plan['fetch'] else 'skipped; remote refs may be stale'}",
        f"auto_safe: {counts['auto_safe']}",
        f"session: {counts['session']}",
        f"owner: {counts['owner']}",
        f"Info: {len(plan['observations'])}",
        f"Check failures: {len(plan['errors'])}",
        f"Result: {'incomplete' if plan['errors'] else 'actions found' if plan['actions'] else 'aligned'} (exit {exit_code(plan)})",
    ]
    for cls in CLASSES:
        lines += ["", f"## {cls}", ""]
        for action in plan["actions"]:
            if action["class"] == cls:
                fact = action["fact"].replace("\n", " ").replace("\r", " ")
                lines.append(
                    f"- **{action['repo']}** | {fact} | Command: ``{action['command']}`` | Risk: {action['risk']}"
                )
    lines += ["", "## Info", ""]
    lines.extend(f"- **{row['repo']}** | {row['fact']}" for row in plan["observations"])
    return "\n".join(lines) + "\n"
