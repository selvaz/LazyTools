"""Capability holds for the local ``review`` approval profile."""

from __future__ import annotations

import ntpath
import re
import shlex
from pathlib import Path

from lazytools.code_bridge._readonly import sensitive_name

_GIT_DENY = {
    "am",
    "checkout",
    "cherry-pick",
    "commit",
    "commit-tree",
    "filter-branch",
    "merge",
    "pull",
    "push",
    "rebase",
    "replace",
    "revert",
    "rm",
    "switch",
    "symbolic-ref",
    "update-ref",
    "worktree",
}
_GIT_SPECIAL = {"branch", "clean", "config", "fetch", "gc", "reflog", "remote", "reset", "restore", "stash", "tag"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "cmd"}
_REMOVERS = {"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri", "unlink", "shred"}
_WRITERS = {
    "touch",
    "mkdir",
    "install",
    "cp",
    "mv",
    "tee",
    "truncate",
    "ln",
    "dd",
    "rsync",
    "set-content",
    "add-content",
    "out-file",
    "copy-item",
    "move-item",
    "new-item",
    "chmod",
    "copy",
    "move",
    "xcopy",
    "robocopy",
    "ren",
    "rename",
    "mklink",
    "setx",
    "icacls",
    "takeown",
    "rename-item",
    "clear-content",
    "expand-archive",
    "compress-archive",
}
_SCHEDULE = {
    "crontab",
    "at",
    "schtasks",
    "launchctl",
    "register-scheduledtask",
    "start-scheduledtask",
    "unregister-scheduledtask",
}
_SERVICE = {
    "systemctl",
    "service",
    "sc",
    "net",
    "start-service",
    "stop-service",
    "restart-service",
    "new-service",
    "set-service",
}
_PUBLISHERS = {
    "npm",
    "pnpm",
    "yarn",
    "cargo",
    "poetry",
    "uv",
    "hatch",
    "flit",
    "pdm",
    "maturin",
    "nuget",
    "helm",
    "vsce",
    "ovsx",
    "twine",
    "gem",
}
_GH_READ = {
    "pr": {"view", "list", "diff", "checks", "status"},
    "issue": {"view", "list", "status"},
    "repo": {"view", "list"},
    "run": {"view", "list", "watch"},
    "workflow": {"view", "list"},
    "release": {"view", "list", "download"},
    "gist": {"view", "list"},
}
_OPERATORS = {";", "&&", "||", "|", "&", "(", ")", "{", "}"}


def _exe(value: str) -> str:
    name = ntpath.basename(value.replace("\\", "/")).lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _tokens(text: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|(){}<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        # Backslash is a path separator on the primary target platform.  Letting
        # POSIX shlex consume it can turn an escaping path into an innocuous one.
        lexer.escape = ""
        return list(lexer)
    except ValueError:
        return None


def _uncertain(value: str) -> bool:
    return bool(re.search(r"(?:\$|%[^%]+%|`|\*|\?|\[|\]|\{|\})", value))


def _path_denied(value: str, workspace: Path, base: Path, *, removal: bool = False) -> bool:
    if not value or value == "-" or value.startswith("-"):
        return value == "-"
    if "\\" in value or re.match(r"^[A-Za-z]:", value):
        workspace_s = str(workspace)
        base_s = str(base)
        # Compare Windows paths with Windows semantics even when tests run on
        # POSIX.  Relative paths are anchored at the actual execution cwd.
        if not re.match(r"^[A-Za-z]:[\\/]", workspace_s):
            return True
        windows_target = ntpath.normcase(ntpath.abspath(ntpath.join(base_s, value)))
        root = ntpath.normcase(ntpath.abspath(workspace_s))
        try:
            inside = ntpath.commonpath((root, windows_target)) == root
        except ValueError:
            return True
        return (
            not inside
            or any(sensitive_name(part) for part in re.split(r"[\\/]", windows_target))
            or (removal and windows_target == root)
        )
    if value == "~" or value.startswith("~/"):
        return True
    if not value:
        return False
    if _uncertain(value) or re.match(r"^[A-Za-z]:[\\/]", value):
        return True
    raw = Path(value).expanduser()
    try:
        target = (base / raw).resolve() if not raw.is_absolute() else raw.resolve()
        target.relative_to(workspace.resolve())
    except (OSError, RuntimeError, ValueError):
        return True
    return any(sensitive_name(part) for part in target.parts) or (removal and target == workspace.resolve())


def _git_subcommand(words: list[str], start: int) -> tuple[str | None, list[str], str | None]:
    i = start + 1
    while i < len(words):
        item = words[i]
        low = item.lower()
        if item == "-C":
            i += 2
            continue
        if low == "-c":
            value = words[i + 1] if i + 1 < len(words) else ""
            if value.lower().startswith("alias."):
                return None, [], "git alias/config override"
            if re.match(r"(?i)^core\.(?:hooksPath|sshCommand|pager|editor|fsmonitor)=", value):
                return None, [], "git executable/path config override"
            i += 2
        elif low.startswith("-c") and not low.startswith("--"):
            if item[2:].lower().startswith("alias."):
                return None, [], "git alias/config override"
            i += 1
        elif low in {"-C".lower(), "--git-dir", "--work-tree", "--namespace", "--config-env"}:
            value = words[i + 1] if i + 1 < len(words) else ""
            if low == "--config-env" and value.lower().startswith("alias."):
                return None, [], "git alias/config override"
            i += 2
        elif low.startswith("--config-env=alias."):
            return None, [], "git alias/config override"
        elif item.startswith("-"):
            i += 1
        else:
            return low, words[i + 1 :], None
    return None, [], None


def _safe_comment(words: list[str], workspace: Path, base: Path) -> bool:
    low = [w.lower() for w in words]
    return (
        len(words) == 8
        and low[:3] == ["gh", "pr", "comment"]
        and words[3].isdigit()
        and low[4] == "--repo"
        and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", words[5]))
        and low[6] == "--body-file"
        and words[7] != "-"
        and not _path_denied(words[7], workspace, base)
    )


def _gh_denial(words: list[str], workspace: Path, base: Path) -> str | None:
    if _safe_comment(words, workspace, base):
        return None
    args: list[str] = []
    i = 1
    while i < len(words):
        low = words[i].lower()
        if low in {"-r", "--repo", "--hostname"}:
            i += 2
        elif low.startswith(("--repo=", "-r=")):
            i += 1
        else:
            args.append(words[i])
            i += 1
    low_args = [a.lower() for a in args]
    if not low_args:
        return None
    if low_args[0] == "api":
        for j, arg in enumerate(low_args[1:]):
            original = args[j + 1]
            if (
                re.match(r"^-(?:f|F|if).+", original)
                or arg in {"-f", "--field", "--raw-field", "--input"}
                or arg.startswith(("--field=", "--raw-field=", "--input="))
            ):
                return "gh api write input"
            method_match = re.match(r"^-x=?(.+)$", arg)
            if method_match and method_match.group(1) != "get":
                return "gh api non-GET method"
            if arg in {"-x", "--method"} and (j + 2 >= len(low_args) or low_args[j + 2] != "get"):
                return "gh api non-GET method"
            if arg.startswith("--method=") and not arg.endswith("=get"):
                return "gh api non-GET method"
        return None
    if low_args[0] == "pr" and len(low_args) > 1 and low_args[1] == "comment":
        return (
            None
            if _safe_comment(["gh", *args], workspace, base)
            else "GitHub PR comment outside the one-comment exception"
        )
    if low_args[:2] == ["auth", "status"] or low_args[0] == "search":
        return None
    if low_args[0] in _GH_READ and len(low_args) > 1 and low_args[1] in _GH_READ[low_args[0]]:
        return None
    return "GitHub remote mutation"


def review_denial(command: str, cwd: str, execution_cwd: str | None = None) -> str | None:
    """Return a held capability; ``cwd`` is always the fixed workspace boundary."""
    words = _tokens(command)
    if not words or not cwd:
        return "unparseable command or unknown workspace"
    windows_workspace = bool(re.match(r"^[A-Za-z]:[\\/]", cwd))
    workspace = Path(cwd) if windows_workspace else Path(cwd).resolve()
    base_value = execution_cwd or cwd
    base = Path(base_value) if windows_workspace else Path(base_value).resolve()
    if any(mark in command for mark in ("$(`", "$(", "${", "$env:", "%TEMP%", "%HOME%", "`")):
        return "dynamic shell expansion"
    if re.search(r"(?i)(?:^|\s)(?:cat|type|get-content)\s+[^;&|]*(?:\.ssh|\.aws[\\/]credentials|hosts\.yml)", command):
        return "credential file read"
    if "<<<" in command and any(re.search(rf"(?i)(?:^|\s){name}(?:\s|$)", command) for name in _SHELLS):
        return "here-string into command interpreter"
    if re.search(r"(?i)(?:^|[;&|]\s*|\b(?:do|then)\s+)\$[A-Za-z_][A-Za-z0-9_]*\b", command):
        return "dynamic command word"
    if re.search(r"(?i)\bgit\s+\$", command):
        return "dynamic git subcommand"
    if re.search(r"(?i)\balias\s+[^;&|]+", command):
        return "shell alias definition"
    if re.search(r"(?i)\[(?:system\.)?io\.file\]::(?:write|delete)", command):
        return ".NET file mutation"
    for i, token in enumerate(words):
        exe = _exe(token)
        if exe in _SHELLS:
            for j in range(i + 1, len(words)):
                flag = words[j].lower()
                if flag == "/c" or flag == "-c" or (flag.startswith("-") and "c" in flag[1:]):
                    if j + 1 < len(words):
                        denial = review_denial(" ".join(words[j + 1 :]), cwd, str(base))
                        if denial:
                            return denial
                    break
        if exe in {"powershell", "pwsh"}:
            for j in range(i + 1, len(words)):
                low = words[j].lower()
                if low in {"-command", "-c"} or low.startswith("-command:"):
                    nested = words[j].split(":", 1)[1] if ":" in words[j] else " ".join(words[j + 1 :])
                    denial = review_denial(nested, cwd, str(base))
                    if denial:
                        return denial
                    break
        if exe in {"eval", "iex", "invoke-expression"}:
            return "dynamic command evaluation"
        if exe in {"invoke-webrequest", "invoke-restmethod"}:
            return "remote publishing or download"
    if "|" in words and any(_exe(w) in _SHELLS for w in words[words.index("|") + 1 :]):
        return "pipe into command interpreter"
    segments: list[list[str]] = [[]]
    for word in words:
        if word in _OPERATORS:
            if segments[-1]:
                segments.append([])
        elif word not in {">", ">>", ">|", "<", "<<"}:
            segments[-1].append(word)
    for segment in segments:
        for i, token in enumerate(segment):
            exe = _exe(token)
            tail = segment[i:]
            segment_low = [w.lower() for w in tail]
            if exe in {"cd", "pushd", "popd"}:
                args = tail[1:]
                target = next((v for v in args if not v.startswith("-") or v == "-"), None)
                if exe == "popd" or target is None or target == "-" or _path_denied(target, workspace, base):
                    return "directory change outside workspace"
                base = (base / target).resolve()
            if exe == "git":
                # Validate -C before parsing the verb, then use it as the base
                # for any later path checks.
                for j, value in enumerate(tail[:-1]):
                    if value == "-C" and _path_denied(tail[j + 1], workspace, base):
                        return "git working-directory override"
                verb, args, error = _git_subcommand(segment, i)
                if error:
                    return error
                if verb in _GIT_DENY and not (verb == "worktree" and args[:1] == ["list"]):
                    return f"git {verb}"
                read_special = (
                    (
                        verb == "branch"
                        and not any(
                            a in {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy"} for a in args
                        )
                    )
                    or (verb == "tag" and not any(a in {"-d", "--delete", "-f", "--force", "-a", "-s"} for a in args))
                    or (verb == "stash" and bool(args) and args[0] in {"list", "show"})
                    or (verb == "remote" and (args[:1] in [["-v"], ["--verbose"], ["get-url"], ["show"]]))
                    or verb in {"reflog"}
                    or (verb == "worktree" and args[:1] == ["list"])
                    or (verb == "config" and any(a in {"--get", "--get-all", "--list", "-l"} for a in args))
                    or (verb == "fetch" and not any(":" in a for a in args if not a.startswith("-")))
                )
                if read_special:
                    pass
                elif verb in _GIT_SPECIAL:
                    return f"git {verb} mutation"
            if exe == "gh":
                denial = _gh_denial(tail, workspace, base)
                if denial:
                    return denial
            if exe in _SCHEDULE:
                return "scheduled task control"
            if exe in _SERVICE and not (
                exe == "net" and len(segment_low) > 1 and segment_low[1] not in {"start", "stop"}
            ):
                return "service control"
            if exe in {"sudo", "doas"}:
                return "sandbox escalation"
            if exe in _REMOVERS and any(
                _path_denied(v, workspace, base, removal=True) for v in tail[1:] if v.lower() not in {"/s", "/q", "/f"}
            ):
                return "removal outside workspace or credential path"
            if exe == "find" and any(v in {"-delete", "-exec"} for v in segment_low):
                return "find removal"
            if exe == "xargs" and any(_exe(v) in _REMOVERS for v in tail[1:]):
                return "xargs removal"
            if exe in _WRITERS:
                operands = [
                    v.split("=", 1)[-1] if v.startswith(("--target-directory=", "of=")) else v for v in tail[1:]
                ]
                if any(_path_denied(v, workspace, base) for v in operands):
                    return "write outside workspace or credential path"
            if exe == "reg" and any(v in {"add", "delete", "copy", "import", "restore"} for v in segment_low[1:]):
                return "registry mutation"
            if exe in {"tar", "unzip", "zip"} and any(
                _path_denied(tail[j + 1], workspace, base)
                for j, v in enumerate(segment_low[:-1])
                if v in {"-c", "-C", "-d", "-o"}
            ):
                return "archive write outside workspace"
            if exe == "wget" and any(
                v in {"-p", "-P", "--directory-prefix"} or v.startswith("--directory-prefix=") for v in tail[1:]
            ):
                return "download write"
            if exe == "find" and any(v in {"-fprint", "-fprint0", "-fls"} for v in segment_low):
                return "find output write"
            if exe == "sed" and re.search(r"(?:^|[; ])w\s+[/~.]", " ".join(tail[1:])):
                return "sed output write"
            if (
                exe == "git"
                and verb in {"clone", "init", "archive", "bundle", "diff"}
                and any(
                    _path_denied(v.split("=", 1)[-1], workspace, base)
                    for v in args
                    if v.startswith(("--output=", "-o", "/", ".."))
                )
            ):
                return "git write outside workspace"
            if (
                exe in {"sed", "perl"}
                and any("i" in v.lstrip("-") for v in tail[1:] if v.startswith("-"))
                and any(_path_denied(v, workspace, base) for v in tail[1:] if not v.startswith("-"))
            ):
                return "in-place write outside workspace"
            if exe in {"curl", "wget"} and any(
                v in {"-o", "--output", "-O"} or v.startswith("--output=") for v in tail[1:]
            ):
                return "download write"
            if exe == "curl" and any(
                v in {"-X", "-d", "--data", "-T", "-F", "--upload-file"}
                or v.startswith(("-X", "--data=", "--upload-file="))
                for v in tail[1:]
            ):
                return "remote publishing"
            if exe == "wget" and any(v.startswith("--post-") for v in tail[1:]):
                return "remote publishing"
            if exe in {"mvn", "gradle", "gradlew", "setup.py"} and any(
                v in {"deploy", "publish", "upload"} for v in segment_low[1:]
            ):
                return "package publishing"
            if exe in {"powershell", "pwsh"} and any(
                v in {"invoke-restmethod", "invoke-webrequest"} for v in segment_low
            ):
                return "remote publishing or download"
            if exe == "npm" and "install" in segment_low and any(v in {"-g", "--global"} for v in segment_low):
                return "global package install"
            if exe in _PUBLISHERS and any(
                v in {"publish", "push", "upload", "unpublish", "deprecate", "dist-tag", "owner", "login", "yank"}
                for v in segment_low[1:]
            ):
                return "package publishing"
            if (exe in {"docker", "podman"} and any(v in {"push", "run"} for v in segment_low[1:])) or (
                exe == "docker" and "--push" in segment_low
            ):
                return "remote/container mutation"
            if exe in {"kubectl", "terraform"} and "apply" in segment_low[1:]:
                return "infrastructure mutation"
    for match in re.finditer(r"(?:^|\s|&)(?:\d*)?(?:>>?|>\|)\s*([^\s;&|]+)", command):
        target = match.group(1).strip("'\"")
        if target not in {"/dev/null", "nul"} and _path_denied(target, workspace, base):
            return "redirect outside workspace or credential path"
    for match in re.finditer(r"(?:^|\s)&?>&?\s*([^\s;&|]+)", command):
        target = match.group(1).strip("'\"")
        if target not in {"/dev/null", "nul"} and _path_denied(target, workspace, base):
            return "redirect outside workspace or credential path"
    return None


__all__ = ["review_denial"]
