"""Fail-closed, tokenized command allow-list for the ``review`` profile."""
from __future__ import annotations
import re
import shlex

_SEPARATORS = {";", "&&", "||", "|"}
_META = {"&", "(", ")", "{", "}", ">", ">>", ">|", "<", "<<", "<<<"}
_SENSITIVE = re.compile(r"(?:^|[/\\])(?:\.env(?:\..*)?|\.git-credentials|\.netrc|id_rsa|id_ed25519|auth\.json|hosts\.yml|secrets?\.json|private\.key|client\.pem|shadow|\.bash_history)$", re.I)

def _tokens(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        result = list(lexer)
    except ValueError:
        return None
    # Expansion and multiline shell programs are intentionally outside the grammar.
    if not result or any(c in command for c in ("$", "`", "\x00", "\n", "\r")):
        return None
    return result

def _operand(word: str) -> bool:
    normalized = word.replace("\\", "/").replace(":", "/").rstrip("/")
    parts = normalized.lower().split("/")
    sensitive = (
        _SENSITIVE.search(normalized)
        or any(part in {".ssh", ".aws", ".config", "credentials"} for part in parts)
        or parts[-1].endswith(".env")
    )
    return not sensitive and not any(c in word for c in "*?[]{}")

def _args(words: list[str], flags: set[str], valued: set[str] | None = None) -> bool:
    valued = valued or set()
    i = 0
    while i < len(words):
        word = words[i]
        if not _operand(word): return False
        if word == "--": return all(_operand(v) for v in words[i + 1:])
        if word.startswith("-") and word != "-":
            if re.fullmatch(r"-\d+", word) and "-n" in flags:
                i += 1
                continue
            name = word.split("=", 1)[0]
            if name not in flags: return False
            if name in valued and "=" not in word:
                i += 1
                if i >= len(words) or not _operand(words[i]): return False
        i += 1
    return True

_GIT = {
 "status": ({"--short","-s","--porcelain","--branch","-b","--show-stash","--ahead-behind","--untracked-files","-uno"}, set()),
 "log": ({"--oneline","--decorate","--stat","--patch","-p","--all","--graph","--no-merges","--name-only","--name-status","--reverse","-n","--max-count","--since","--until","--format","--pretty"}, {"-n","--max-count","--since","--until","--format","--pretty"}),
 "diff": ({"--check","--stat","--name-only","--name-status","--cached","--staged","--color","--no-color","-w","--word-diff","-U"}, {"-U"}),
 "show": ({"--stat","--oneline","--name-only","--name-status","--format","--pretty","--color","--no-color","-s"}, {"--format","--pretty"}),
 "rev-parse": ({"--verify","--short","--show-toplevel","--show-prefix","--show-cdup","--is-inside-work-tree","--abbrev-ref","--symbolic-full-name"}, {"--short"}),
}
def _git(words: list[str]) -> bool:
    if len(words) < 2 or words[1].startswith("-"): return False
    verb, rest = words[1].lower(), words[2:]
    if verb in _GIT: return _args(rest, *_GIT[verb])
    if verb == "fetch":
        flags={"--all","--prune","--tags","--no-tags","--dry-run","--verbose","-v","--quiet","-q","--force","-f"}
        return _args(rest, flags) and not any(":" in v for v in rest)
    return False

_GH = {
 "view": ({"--comments","--json","--jq","--template","--web","-w"},{"--json","--jq","--template"}),
 "diff": ({"--color","--name-only","--patch","--web","-w"},{"--color"}),
 "checks": ({"--fail-fast","--interval","--json","--jq","--template","--watch","--web","-w"},{"--interval","--json","--jq","--template"}),
}
def _gh(words: list[str]) -> bool:
    if len(words) < 3 or words[1].lower() != "pr" or words[2].lower() not in _GH: return False
    flags, valued = _GH[words[2].lower()]
    return _args(words[3:], flags | {"--repo","-R"}, valued | {"--repo","-R"})

def _allowed(words: list[str]) -> bool:
    if not words or not all(_operand(w) for w in words): return False
    exe = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if exe == "git": return _git(words)
    if exe == "gh": return _gh(words)
    if exe in {"pytest", "py.test"}:
        return _args(words[1:], {"-q","-v","-x","-s","--disable-warnings","--tb","-k","-m","--maxfail"}, {"--tb","-k","-m","--maxfail"})
    if exe in {"python", "python3"}: return len(words) >= 3 and words[1:3] == ["-m","pytest"] and _allowed(["pytest",*words[3:]])
    if exe == "npm": return words[1:] == ["test"] or words[1:] in (["run","build"],["run","lint"])
    flags = {
      "ls":{"-a","-A","-l","-h","-la","-al","-lh","-lah","--all","--long","--human-readable"},
      "cat":{"-n","-b","-s","-v","-E","-T","--number"},
      "grep":{"-n","-i","-v","-r","-R","-E","-F","-w","-x","-l","-c","-q","--line-number","--ignore-case","--recursive"},
      "head":{"-n","-c","-q","-v","--lines","--bytes"}, "tail":{"-n","-c","-q","-v","--lines","--bytes"},
    }
    valued={"head":{"-n","-c","--lines","--bytes"},"tail":{"-n","-c","--lines","--bytes"}}
    return exe in flags and _args(words[1:], flags[exe], valued.get(exe))

def review_denial(command: str, cwd: str, execution_cwd: str | None = None, scratch_dir: str | None = None) -> str | None:
    """Return ``None`` only if every parsed command segment is allow-listed."""
    del execution_cwd, scratch_dir
    if not cwd: return "unknown workspace"
    tokens = _tokens(command)
    if not tokens: return "unparseable command"
    segments: list[list[str]]=[[]]
    for token in tokens:
        if token in _META: return "unsupported shell syntax"
        if token in _SEPARATORS:
            if not segments[-1]: return "ambiguous shell syntax"
            segments.append([])
        else: segments[-1].append(token)
    if not segments[-1] or any(not _allowed(s) for s in segments): return "command is not in the review read-only allow-list"
    return None

__all__ = ["review_denial"]
