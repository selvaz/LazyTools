"""Structural approval helpers for the code bridge: explicit classifiers, not more fnmatch.

Three things a ``TieredGate`` rule table cannot express safely with ``fnmatch``
alone, each implemented here as a small, unit-tested predicate:

* :func:`is_readonly_gh_query` -- a Codex sandbox escalation that is purely a
  read-only GitHub query (``gh pr view|checks|diff``, ``gh run view|list``,
  ``gh api`` GET on ``repos/...``), optionally wrapped in
  ``powershell.exe -Command '...'``. The check is an ALLOW-list over a strict
  tokenisation: anything it does not positively recognise is "not read-only"
  and falls through to the table's catch-all ``ask``. A false "ask" is cheap;
  a false "allow" is not.
* :func:`is_simple_git_add` / :func:`is_simple_git_commit` -- an escalated
  ``git add`` / ``git commit`` that is ONE simple command (no chaining, no
  substitution, no redirection), so the session grant a human gives on the
  first one covers exactly that form and nothing chained onto it.
* :func:`strip_literal_heredocs` -- the Claude ``Bash`` text with the BODY of
  a literal heredoc written to a file by ``cat``/``tee`` removed, so file
  content (``delete=False`` in a Python script) is not mistaken for a command.

:class:`ClassifiedRule` plugs a classifier into the ordinary rule table, and
:class:`BridgeGate` (a ``TieredGate``) evaluates those rules on the WHOLE
command before the stock segment-by-segment matching runs. ``TieredGate``
splits on ``|`` and newlines, which is right for its fnmatch rules but would
cut a quoted ``--jq '... | ...'`` filter in half; the classifier does its own
quote-aware tokenisation instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from fnmatch import fnmatch
from typing import Any

from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import Rule, TieredGate

# --------------------------------------------------------------------------- #
# strict tokenisation
# --------------------------------------------------------------------------- #

#: Characters allowed in an UNQUOTED word. Deliberately excludes every shell
#: or PowerShell metacharacter: ``; & | < > ( ) { } $ ` @ # % ! * ? [ ] ~ \``
#: and whitespace other than the separating space/tab. ``$``/backtick/``\``
#: are also refused anywhere in the command (see ``_FORBIDDEN_ANYWHERE``).
_UNQUOTED = re.compile(r"[A-Za-z0-9_./:,=+\-]")

#: Refused anywhere, quoted or not: substitution/escape characters of bash
#: (``$``, backtick, ``\``) and PowerShell (``$``, backtick), plus anything
#: that is not printable ASCII -- PowerShell treats Unicode "smart" quotes
#: and dashes as their ASCII equivalents, so a non-ASCII character could end
#: a quoted string this tokeniser believes is still open.
_FORBIDDEN_ANYWHERE = re.compile(r"[$`\\]|[^\x20-\x7e]")


@dataclass(frozen=True)
class _Word:
    text: str
    #: True if any part of the word was inside quotes. Subcommand words, flag
    #: names and positionals must be unquoted; only flag VALUES may be quoted.
    quoted: bool


def _tokenize(command: str) -> list[_Word] | None:
    """Split a single simple command into words, or ``None`` if it is not one.

    Quote-aware for ``'...'`` and ``"..."`` (both literal here, because
    ``$``/backtick/``\\`` are refused outright). Returns ``None`` -- "not a
    simple command" -- for any unquoted character outside :data:`_UNQUOTED`,
    which is how chaining (``;`` ``&&`` ``||`` ``|``), redirection (``>``
    ``<``), subshells and globbing are all refused without enumerating them.
    """
    if _FORBIDDEN_ANYWHERE.search(command):
        return None
    words: list[_Word] = []
    buf: list[str] = []
    quoted = False
    in_word = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch in " \t":
            if in_word:
                words.append(_Word("".join(buf), quoted))
                buf, quoted, in_word = [], False, False
            i += 1
            continue
        if ch in "'\"":
            end = command.find(ch, i + 1)
            if end < 0:
                return None  # unterminated quote
            buf.append(command[i + 1 : end])
            quoted = in_word = True
            i = end + 1
            continue
        if not _UNQUOTED.fullmatch(ch):
            return None
        buf.append(ch)
        in_word = True
        i += 1
    if in_word:
        words.append(_Word("".join(buf), quoted))
    return words


#: ``powershell.exe -Command '<inner>'`` (or ``pwsh``, or the double-quoted
#: form), with at most the harmless startup switches, and NOTHING after the
#: closing quote. The inner text may not contain the outer quote character at
#: all -- PowerShell's ``''`` escape is not honoured here, so a quote inside
#: the payload always means "not this shape" (fail closed).
_POWERSHELL_WRAPPER = re.compile(
    r"""^(?:powershell|pwsh)(?:\.exe)?
        (?:\s+-(?:NoProfile|NonInteractive|NoLogo))*
        \s+-(?:Command|c)\s+
        (?:'(?P<single>[^']*)'|"(?P<double>[^"]*)")
        \s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def unwrap_powershell(command: str) -> str:
    """The payload of a well-formed ``powershell.exe -Command '...'`` wrapper, else ``command`` unchanged."""
    match = _POWERSHELL_WRAPPER.match(command.strip())
    if match is None:
        return command
    inner = match.group("single") if match.group("single") is not None else match.group("double")
    return inner.strip()


# --------------------------------------------------------------------------- #
# read-only gh queries
# --------------------------------------------------------------------------- #

_OUTPUT_FLAGS = frozenset({"--json", "--jq", "-q", "--template", "-t"})
_REPO_FLAGS = frozenset({"--repo", "-R"})


@dataclass(frozen=True)
class _GhShape:
    #: Flags that consume the next word (or ``--flag=value``).
    value_flags: frozenset[str]
    #: Flags that stand alone.
    bool_flags: frozenset[str]
    #: Maximum number of positional arguments after the subcommand.
    max_positionals: int


#: Every subcommand auto-allowed, with every flag it may carry. A flag not
#: listed here (``--web``, ``-X``, ``-f``, ``--input``, ``-H``, ...) makes the
#: command "not read-only". ``gh api`` additionally requires its single
#: positional to be a ``repos/...`` path (see :func:`_api_endpoint_ok`).
_GH_READONLY: dict[tuple[str, ...], _GhShape] = {
    ("pr", "view"): _GhShape(_OUTPUT_FLAGS | _REPO_FLAGS, frozenset({"--comments", "-c"}), 1),
    ("pr", "checks"): _GhShape(
        _OUTPUT_FLAGS | _REPO_FLAGS | {"--interval", "-i"}, frozenset({"--required", "--fail-fast", "--watch"}), 1
    ),
    ("pr", "diff"): _GhShape(_REPO_FLAGS | {"--color"}, frozenset({"--name-only", "--patch"}), 1),
    ("run", "view"): _GhShape(
        _OUTPUT_FLAGS | _REPO_FLAGS | {"--job", "-j", "--attempt", "-a"},
        frozenset({"--log", "--log-failed", "--exit-status", "--verbose", "-v"}),
        1,
    ),
    ("run", "list"): _GhShape(
        _OUTPUT_FLAGS
        | _REPO_FLAGS
        | {
            "--limit", "-L", "--branch", "-b", "--workflow", "-w", "--status", "-s",
            "--event", "-e", "--user", "-u", "--commit", "-c", "--created",
        },
        frozenset({"--all", "-a"}),
        0,
    ),
    # No -X/--method, -f/-F/--field/--raw-field, --input, -H/--header: with
    # none of them gh sends a GET. Only output shaping and pagination.
    ("api",): _GhShape(
        frozenset({"--jq", "-q", "--template", "-t", "--cache"}),
        frozenset({"--paginate", "--slurp", "--include", "-i", "--silent"}),
        1,
    ),
}  # fmt: skip

_API_ENDPOINT = re.compile(r"/?repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?")


def _api_endpoint_ok(endpoint: str) -> bool:
    return bool(_API_ENDPOINT.fullmatch(endpoint)) and ".." not in endpoint.split("/")


def is_readonly_gh_query(command: str) -> bool:
    """True only for ONE read-only ``gh`` query, optionally inside ``powershell.exe -Command '...'``.

    Everything else is ``False``: a compound command, any redirection or
    substitution, a ``gh api`` call that could be a write, any other ``gh``
    subcommand, and anything that is not ``gh`` at all.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    words = _tokenize(unwrap_powershell(command))
    if not words or words[0].quoted or words[0].text.lower() not in ("gh", "gh.exe"):
        return False
    rest = words[1:]
    shape: _GhShape | None = None
    for prefix, candidate in _GH_READONLY.items():
        head = rest[: len(prefix)]
        if len(head) == len(prefix) and all(not w.quoted and w.text == p for w, p in zip(head, prefix, strict=True)):
            shape, rest = candidate, rest[len(prefix) :]
            break
    if shape is None:
        return False
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        word = rest[i]
        if word.text.startswith("-"):
            if word.quoted:
                return False
            name, has_inline, _value = word.text.partition("=")
            if name in shape.value_flags:
                if not has_inline:
                    if i + 1 >= len(rest):
                        return False  # value flag with nothing after it
                    i += 1  # consume the value; it may be quoted (a --jq filter)
            elif name in shape.bool_flags and not has_inline:
                pass
            else:
                return False  # unknown flag: -X, --method, -f, --input, --web, ...
        else:
            if word.quoted or not word.text:
                return False
            positionals.append(word.text)
        i += 1
    if len(positionals) > shape.max_positionals:
        return False
    if shape is _GH_READONLY[("api",)]:
        return len(positionals) == 1 and _api_endpoint_ok(positionals[0])
    return True


# --------------------------------------------------------------------------- #
# simple git add / git commit escalations
# --------------------------------------------------------------------------- #


def _simple_git(command: str, subcommand: str) -> list[_Word] | None:
    words = _tokenize(unwrap_powershell(command))
    if not words or len(words) < 2:
        return None
    # No global options before the subcommand ("git -C elsewhere add ..."):
    # the subcommand must be the second word, and unquoted.
    if words[0].quoted or words[0].text.lower() not in ("git", "git.exe"):
        return None
    if words[1].quoted or words[1].text != subcommand:
        return None
    return words[2:]


def is_simple_git_add(command: str) -> bool:
    """One unchained ``git add ...`` (powershell-wrapped or not)."""
    return _simple_git(command, "add") is not None


def is_simple_git_commit(command: str) -> bool:
    """One unchained ``git commit ...`` that neither rewrites a commit nor skips hooks."""
    args = _simple_git(command, "commit")
    if args is None:
        return False
    for word in args:
        if word.quoted or not word.text.startswith("-"):
            continue  # a message or a pathspec
        name = word.text.partition("=")[0]
        if name in ("--amend", "--no-verify"):
            return False
        # A short-option cluster carrying -n (= --no-verify), e.g. "-nm".
        if re.fullmatch(r"-[A-Za-z]+", name) and "n" in name[1:]:
            return False
    return True


#: Registry of classifiers a :class:`ClassifiedRule` may name. A name, not a
#: callable, so the rule stays a plain frozen, hashable, printable value and
#: its fingerprint (which scopes session grants) is stable across processes.
CLASSIFIERS: dict[str, Callable[[str], bool]] = {
    "readonly_gh_query": is_readonly_gh_query,
    "simple_git_add": is_simple_git_add,
    "simple_git_commit": is_simple_git_commit,
}


@dataclass(frozen=True)
class ClassifiedRule(Rule):
    """A rule decided by a named classifier over the WHOLE command text.

    It matches only when the command it is shown is the request's complete
    original command (``request.raw["command"]``) -- never a segment the
    stock ``TieredGate`` splitter cut out of a compound command. So even
    under a plain ``TieredGate`` (which matches segment by segment) a
    classified ``allow`` can never be earned by one harmless-looking piece of
    ``gh pr view 1; gh pr merge 1``: the pieces are not the whole command,
    so they fall through to the next rule. :class:`BridgeGate` consults these
    rules on the whole command first.
    """

    classifier: str = ""

    def __post_init__(self) -> None:
        if self.classifier not in CLASSIFIERS:
            raise ValueError(f"unknown classifier {self.classifier!r}; one of {sorted(CLASSIFIERS)}")
        if self.arg_pattern is not None:
            raise ValueError("a ClassifiedRule is decided by its classifier, not an arg_pattern")

    def matches(self, request: ApprovalRequest) -> bool:
        if not fnmatch(str(request.provider), self.provider_pattern):
            return False
        if not fnmatch(str(request.kind), self.kind_pattern):
            return False
        if not fnmatch(request.name, self.name_pattern):
            return False
        command = request.arguments.get("command")
        # Codex passes its wire params as ``raw``; a request without a
        # verifiable original command never matches (fail closed).
        original = request.raw.get("command")
        if not isinstance(command, str) or command != original:
            return False  # a segment of a compound command, or no verifiable command text
        return CLASSIFIERS[self.classifier](command)

    def fingerprint(self) -> str:
        import hashlib

        payload = f"{super().fingerprint()}|classifier={self.classifier}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# heredoc bodies (Claude Bash)
# --------------------------------------------------------------------------- #

#: A heredoc delimiter: fully single-quoted, fully double-quoted, or a plain
#: identifier. Partially quoted forms (``E"O"F``, ``\\EOF``) are not
#: recognised and so never stripped.
_DELIM = r"""(?:'(?P<sq>[A-Za-z_][A-Za-z0-9_]*)'|"(?P<dq>[A-Za-z_][A-Za-z0-9_]*)"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"""
#: A redirect target / tee path: no quoting, no metacharacters, no substitution.
_PATH = r"[A-Za-z0-9_./~:+-][A-Za-z0-9_./~:+=,@-]*"
_HEREDOC_SHAPE = r"""<<-?\s*(?:'[A-Za-z_][A-Za-z0-9_]*'|"[A-Za-z_][A-Za-z0-9_]*"|[A-Za-z_][A-Za-z0-9_]*)"""
_HEREDOC = re.compile(rf"<<(?P<dash>-)?\s*{_DELIM}")
#: The ONLY heredoc-consuming commands whose body is stripped: a pure data
#: sink writing the body to a file. ``bash <<EOF``, ``python - <<EOF``,
#: ``cat <<EOF | sh`` and the like are code and are left fully visible.
_SINK = re.compile(
    rf"""^(?:
        cat\s+(?:>>?\s*{_PATH}\s+)?{_HEREDOC_SHAPE}(?:\s+>>?\s*{_PATH})?
      | tee(?:\s+-a)?(?:\s+{_PATH})+\s+{_HEREDOC_SHAPE}(?:\s+>\s*/dev/null)?
    )$""",
    re.VERBOSE,
)
_LINE_SEPARATORS = re.compile(r"&&|\|\||;")


def _sink_heredoc(line: str) -> tuple[str, bool, bool] | None:
    """``(delimiter, dash, quoted)`` if ``line`` ends in a data-sink heredoc, else ``None``.

    Earlier ``&&``/``;``/``||`` segments on the same line are allowed (they
    stay in the command and are matched as usual); the LAST segment must be
    exactly a ``cat``/``tee`` sink, and the line may contain no ``|`` pipe
    at all and only one ``<<``.
    """
    if line.count("<<") != 1 or re.search(r"(?<!\|)\|(?!\|)", line):
        return None
    last = _LINE_SEPARATORS.split(line)[-1].strip()
    if _SINK.match(last) is None:
        return None
    match = _HEREDOC.search(last)
    if match is None:  # pragma: no cover - the sink shape guarantees one
        return None
    delimiter = match.group("sq") or match.group("dq") or match.group("bare")
    quoted = match.group("bare") is None
    return delimiter, bool(match.group("dash")), quoted


def strip_literal_heredocs(command: str) -> str:
    """``command`` with the bodies of literal, data-sink heredocs removed.

    A body is removed only when ALL hold:

    * the heredoc feeds ``cat`` / ``tee`` writing to a file (a data sink),
      never an interpreter (``bash``, ``sh``, ``python``, ...);
    * its delimiter is quoted (``<<'EOF'``: the body is literal), or the body
      contains no ``$``, backtick or backslash (an unquoted ``<<EOF`` body
      would otherwise run ``$( )`` substitutions);
    * the terminating delimiter line is actually found.

    The ``cat > file <<'EOF'`` line itself and everything after the
    terminator stay in the text and are matched as usual. When in doubt the
    body is kept, which can only produce an extra ``ask``.
    """
    if "<<" not in command:
        return command
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        found = _sink_heredoc(line)
        if found is None:
            continue
        delimiter, dash, quoted = found
        end = None
        for j in range(i, len(lines)):
            candidate = lines[j].lstrip("\t") if dash else lines[j]
            if candidate == delimiter:
                end = j
                break
        if end is None:
            continue  # no terminator: keep everything visible
        body = lines[i:end]
        if not quoted and any(ch in text for text in body for ch in "$`\\"):
            continue  # an expanding body can run substitutions: keep it visible
        out.append(lines[end])  # the terminator line, harmless and kept for readability
        i = end + 1
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


@dataclass
class BridgeGate(TieredGate):
    """``TieredGate`` plus the two structural steps above.

    1. :class:`ClassifiedRule` entries are tried first, on the request's
       whole, unsplit command (first match in table order wins).
    2. For Claude's ``Bash`` tool, literal data-sink heredoc bodies are
       removed before the stock segment matching (:func:`strip_literal_heredocs`).

    Everything else -- the audit record, the prompt shown to a human (always
    the ORIGINAL command), session-grant scoping -- is ``TieredGate``'s own.
    """

    def _match(self, request: ApprovalRequest) -> Rule | None:
        command = request.arguments.get("command")
        if isinstance(command, str):
            for rule in self.rules:
                if isinstance(rule, ClassifiedRule) and rule.matches(request):
                    return rule
            if request.kind == "tool" and request.name == "Bash":
                stripped = strip_literal_heredocs(command)
                if stripped != command:
                    arguments: dict[str, Any] = {**dict(request.arguments), "command": stripped}
                    request = replace(request, arguments=arguments)
        return super()._match(request)


__all__ = [
    "CLASSIFIERS",
    "BridgeGate",
    "ClassifiedRule",
    "is_readonly_gh_query",
    "is_simple_git_add",
    "is_simple_git_commit",
    "strip_literal_heredocs",
    "unwrap_powershell",
]
