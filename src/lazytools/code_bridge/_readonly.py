"""Whole-command, fail-closed Claude Bash capability allow-list.

No shell evaluation or normalization of wrappers. Unknown syntax and options
ask; pipelines require a read-only match for every stage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

from lazytools.code_bridge._allowlist import load_rules


def pipeline(command: str) -> list[str] | None:
    # Refuse expansion, escapes, comments and control/non-ASCII text everywhere.
    if not command or re.search(r"[\\$`#]|[^\x20-\x7e]", command):
        return None
    stages: list[str] = []
    buf = ""
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "<>&|;()" or (ch in "012" and (not buf or buf.endswith(" "))):
            # Consume only complete, unquoted redirects at word boundaries.
            # No arbitrary file targets, input redirects, append or clobber.
            redirect = re.match(r"(?:[012]?> */dev/null|[012]>&[012])(?= |$)", command[i:])
            if redirect and (not buf or buf.endswith(" ")):
                i += len(redirect[0])
                continue
            if ch == "|" and command[i:i + 2] != "||" and command[i - 1:i] != "|":
                if not buf.strip():
                    return None
                stages.append(buf.strip())
                buf = ""
                i += 1
                continue
            if ch in "<>&|;()":
                return None
        buf += ch
        i += 1
    if quote or not buf.strip():
        return None
    stages.append(buf.strip())
    return stages


#: Names (path segments, compared case-insensitively) a Bash read never touches:
#: credential stores and secret material. ``.git`` is included because
#: ``.git/config`` can hold credentialed remote URLs.
SENSITIVE_NAMES = (
    ".env*", "auth.json", ".ssh", ".codex", ".claude", ".aws", ".azure", ".gnupg", ".npmrc", ".pypirc",
    ".netrc", ".git-credentials", "credentials*", "*.pem", "*.key", "id_rsa*", "*secret*", "*token*", ".git",
    "id_ed25519*", "id_ecdsa*", "id_dsa*", ".pgpass", "*.tfstate*", "*.tfvars*", "*.pfx", "*.p12", "kubeconfig",
    ".kube", ".docker",
)

#: Windows device names (with or without an extension): opening one can block.
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9]|conin\$|conout\$)(?:\..*)?", re.IGNORECASE)


def sensitive_name(segment: str) -> bool:
    # NTFS ignores trailing dots/spaces, so ".env." names the same file as ".env".
    name = segment.rstrip(". ").lower()
    if any(fnmatchcase(name, pattern) for pattern in SENSITIVE_NAMES):
        return True
    # A backup copy of a credential file (auth.json.bak, id_rsa.bak.bak) is the same secret.
    return name.endswith(".bak") and sensitive_name(name[: -len(".bak")])


def operand_ok(operand: str, *, git: bool, pathspec: bool = False) -> bool:
    """A read operand that is relative, stays inside the cwd and names no credential file.

    ``git`` operands are revisions or pathspecs (``HEAD:README.md``), so ``:`` is
    a separator there; for filesystem tools any ``:`` (drive letter, alternate
    data stream) asks. Absolute paths (``/dev/zero``, ``/c/Users/...``), ``~``,
    ``..`` segments and stdin (``-``) always ask. Path checks are lexical:
    symlinks and junctions inside the cwd are not resolved (see docs).

    ``pathspec`` operands (every git command except ``branch --list``, whose
    operand is a ref-name pattern) additionally refuse wildcards
    (``* ? [ ] { }``) and a leading ``:`` (pathspec magic such as ``:(top).env``):
    git expands them against the tree, so ``".e*"`` would reach ``.env`` past the
    name check above.
    """
    if pathspec and (operand.startswith(":") or re.search(r"[*?\[\]{}]", operand)):
        return False
    if not operand or operand == "-" or operand.startswith(("/", "~")) or re.match(r"[A-Za-z]:", operand):
        return False
    segments = re.split(r"[/:]" if git else r"/", operand)
    if ".." in segments or (not git and ":" in operand):
        return False
    for segment in segments:
        if sensitive_name(segment):
            return False
        if not git and (re.search(r"~[0-9]", segment) or _DEVICE.fullmatch(segment.rstrip(". "))):
            return False  # 8.3 short-name alias or Windows device name
    return True


def glob_ok(value: str) -> bool:
    """An ``rg -g`` glob. A positive glob overrides hidden/ignore filtering, so it must be narrow.

    Exclusions (``!x``) only narrow the search. A positive glob must name literal,
    non-credential directories and end in a literal file name or ``*.ext``: a
    wildcard-only name (``*``, ``**``, ``.*``) would re-include ``.env``.
    """
    if value.startswith("!"):
        return True
    *dirs, last = value.split("/")
    for segment in dirs:
        if segment != "**" and (not re.fullmatch(r"[A-Za-z0-9_.-]+", segment) or sensitive_name(segment)):
            return False
    if re.fullmatch(r"\*\.[A-Za-z0-9]+", last):
        return not sensitive_name(last)
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", last)) and not sensitive_name(last)


@dataclass(frozen=True)
class ReadRule:
    id: str
    command: tuple[str, ...]
    flags: frozenset[str]
    values: frozenset[str]
    max_positionals: int
    validator: str
    #: ``paths``: every positional is a path/revision; ``pattern-paths``: the
    #: first positional is a search pattern unless ``-e``/``--regexp`` or
    #: ``--files`` is given; ``none``: positionals are not filesystem operands.
    operands: str = "paths"
    #: Flags whose values are ``rg``-style globs (see :func:`glob_ok`).
    globs: frozenset[str] = frozenset()
    #: The tool reads stdin when given no path operand. That is only allowed on
    #: the right side of a pipe, unless one of ``no_stdin_flags`` (``-r``,
    #: ``--files``) makes it search the cwd instead.
    stdin: bool = False
    no_stdin_flags: frozenset[str] = frozenset()

    def matches(self, args: list[str], *, piped: bool = False) -> bool:
        if tuple(args[:len(self.command)]) != self.command:
            return False
        rest = args[len(self.command):]
        positionals: list[str] = []
        seen: set[str] = set()
        i = 0
        paths_only = False
        list_mode = False
        while i < len(rest):
            arg = rest[i]
            if arg == "--" and self.validator != "find":
                paths_only = True
            elif arg.startswith("-") and not paths_only:
                if arg == "--list":
                    list_mode = True
                flag, sep, value = arg.partition("=")
                if flag in self.values:
                    if not sep:
                        i += 1
                        if i == len(rest):
                            return False
                        value = rest[i]
                    if not value or value.startswith("-"):
                        return False
                    if flag in self.globs and not glob_ok(value):
                        return False
                    seen.add(flag)
                elif arg in self.flags or (self.validator == "git-log" and re.fullmatch(r"-[0-9]+", arg)):
                    seen.add(arg)
                else:
                    return False
            else:
                positionals.append(arg)
            i += 1
        if len(positionals) > self.max_positionals:
            return False
        if self.validator == "branch" and positionals and not list_mode:
            return False
        if self.validator == "api":
            from lazytools.code_bridge._approvals import _api_endpoint_ok

            return len(positionals) == 1 and _api_endpoint_ok(positionals[0])
        if self.operands != "none":
            operands = positionals
            if self.operands == "pattern-paths" and not seen & {"-e", "--regexp", "--files"}:
                operands = positionals[1:]  # the first positional is the search pattern, not a path
            git = self.command[0] == "git"
            if not all(operand_ok(op, git=git, pathspec=git and self.validator != "branch") for op in operands):
                return False
            if self.stdin and not operands and not piped and not seen & self.no_stdin_flags:
                return False  # would read stdin and could block forever
        return True


def load_policy(source: Path | None = None) -> tuple[ReadRule, ...]:
    source = source or Path(__file__).with_name("readonly_allowlist.toml")
    fields = {
        "id", "command", "flags", "values", "max_positionals", "validator", "operands", "globs", "stdin", "no_stdin_flags",
    }
    rules = []
    for raw in load_rules(source, fields, required={"id", "command"}):
        for key in ("command", "flags", "values", "globs", "no_stdin_flags"):
            value = raw.get(key, [])
            if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                raise ValueError(f"{source}: {key} must be an array of non-empty strings")
        count, validator = raw.get("max_positionals", 1000), raw.get("validator", "options")
        operands, stdin = raw.get("operands", "paths"), raw.get("stdin", False)
        if (
            not raw["command"] or type(count) is not int or count < 0
            or not isinstance(validator, str) or validator not in {"options", "branch", "api", "find", "git-log"}
            or operands not in {"paths", "pattern-paths", "none"} or type(stdin) is not bool
            or not set(raw.get("globs", [])) <= set(raw.get("values", []))
        ):
            raise ValueError(f"{source}: invalid read-only capability")
        rules.append(ReadRule(
            raw["id"], tuple(raw["command"]), frozenset(raw.get("flags", [])), frozenset(raw.get("values", [])),
            count, validator, operands, frozenset(raw.get("globs", [])), stdin, frozenset(raw.get("no_stdin_flags", [])),
        ))
    return tuple(rules)


@lru_cache(maxsize=1)
def packaged_policy() -> tuple[ReadRule, ...]:
    return load_policy()


def match(command: str) -> str | None:
    from lazytools.code_bridge._approvals import _tokenize

    stages = pipeline(command)
    if stages is None:
        return None
    ids = []
    for index, stage in enumerate(stages):
        words = _tokenize(stage)
        if not words or words[0].quoted:
            return None
        args = [word.text for word in words]
        rule = next((rule for rule in packaged_policy() if rule.matches(args, piped=index > 0)), None)
        if rule is None:
            return None
        ids.append(rule.id)
    return " | ".join(ids)
