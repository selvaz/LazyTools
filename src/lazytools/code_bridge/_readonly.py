"""Whole-command, fail-closed Claude Bash capability allow-list.

No shell evaluation or normalization of wrappers. Unknown syntax and options
ask; pipelines require a read-only match for every stage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ReadRule:
    id: str
    command: tuple[str, ...]
    flags: frozenset[str]
    values: frozenset[str]
    max_positionals: int
    validator: str

    def matches(self, args: list[str]) -> bool:
        if tuple(args[:len(self.command)]) != self.command:
            return False
        rest = args[len(self.command):]
        positionals: list[str] = []
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
                elif arg not in self.flags and not (self.validator == "git-log" and re.fullmatch(r"-[0-9]+", arg)):
                    return False
            else:
                positionals.append(arg)
            i += 1
        if len(positionals) > self.max_positionals:
            return False
        if self.validator == "branch" and positionals and not list_mode:
            return False
        if self.validator == "fetch" and positionals not in ([], ["origin"]):
            return False
        if self.validator == "api":
            from lazytools.code_bridge._approvals import _api_endpoint_ok

            return len(positionals) == 1 and _api_endpoint_ok(positionals[0])
        return True


def load_policy(source: Path | None = None) -> tuple[ReadRule, ...]:
    source = source or Path(__file__).with_name("readonly_allowlist.toml")
    fields = {"id", "command", "flags", "values", "max_positionals", "validator"}
    rules = []
    for raw in load_rules(source, fields, required={"id", "command"}):
        for key in ("command", "flags", "values"):
            value = raw.get(key, [])
            if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                raise ValueError(f"{source}: {key} must be an array of non-empty strings")
        count, validator = raw.get("max_positionals", 1000), raw.get("validator", "options")
        if not raw["command"] or type(count) is not int or count < 0 or not isinstance(validator, str) or validator not in {"options", "branch", "fetch", "api", "find", "git-log"}:
            raise ValueError(f"{source}: invalid read-only capability")
        rules.append(ReadRule(raw["id"], tuple(raw["command"]), frozenset(raw.get("flags", [])), frozenset(raw.get("values", [])), count, validator))
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
    for stage in stages:
        words = _tokenize(stage)
        if not words or words[0].quoted:
            return None
        args = [word.text for word in words]
        rule = next((rule for rule in packaged_policy() if rule.matches(args)), None)
        if rule is None:
            return None
        ids.append(rule.id)
    return " | ".join(ids)
