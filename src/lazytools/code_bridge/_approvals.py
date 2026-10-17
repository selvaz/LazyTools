"""Structural approval helpers for the code bridge: explicit classifiers, not more fnmatch.

Two things a ``TieredGate`` rule table cannot express safely with ``fnmatch``
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
  substitution, no redirection) whose options, after quote removal, all come
  from a short allow-list, so the session grant a human gives on the first
  one covers exactly that form.

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
from dataclasses import dataclass
from fnmatch import fnmatch

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
    ``<``), subshells and globbing are all refused without enumerating them,
    and for any empty argument (see the comment at the end).
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
    # An EMPTY argument ('' or "", alone or after quote removal) means "not a
    # command this module can vouch for". Windows PowerShell 5.1 DROPS empty
    # arguments when it starts a native exe, so the program would receive a
    # different argument list from the one classified here:
    # `gh api --jq '' --template -XDELETE ...` reaches gh as
    # `--jq --template -XDELETE ...` (a DELETE), and `git commit -m '' -m --amend`
    # as `-m -m --amend`. Rather than emulate PowerShell's argument passing,
    # fail closed: every classifier built on this tokeniser rejects it, so it
    # asks. Found by review (round 2).
    if any(not word.text for word in words):
        return None
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


#: The ONLY options a session-granted escalated ``git add`` may carry. Every
#: word is judged AFTER quote removal, so ``"--force"`` is ``--force``. Exact
#: matches only: git accepts unambiguous abbreviations (``--forc``), so a
#: prefix-free deny-list cannot work -- this is an allow-list.
_GIT_ADD_OPTIONS = frozenset({"-A", "--all", "-u", "--update"})


def _repo_relative(path: str) -> bool:
    """A non-empty relative path with no ``..`` component (so it stays inside the repo)."""
    if not path or path == "-" or path.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", path):
        return False
    return ".." not in re.split(r"[/\\]", path)


def is_simple_git_add(command: str) -> bool:
    """One unchained ``git add`` with only paths and ``-A``/``--all``/``-u``/``--update``/``--``."""
    args = _simple_git(command, "add")
    if args is None:
        return False
    paths_only = False
    for word in args:
        text = word.text
        if paths_only or not text.startswith("-"):
            if not text:
                return False
            continue  # a path
        if text == "--":
            paths_only = True
        elif text not in _GIT_ADD_OPTIONS:
            return False  # --force, -f, -p, --chmod=..., abbreviations, anything else
    return True


def is_simple_git_commit(command: str) -> bool:
    """One unchained ``git commit`` whose options are exactly from a short allow-list.

    Allowed, after quote removal: ``-m <msg>``, ``--message=<msg>``, ``-a``,
    ``--all``, ``-am <msg>``, ``-F <file>``, ``--file=<file>`` (file a
    relative path inside the repository), ``--`` and repo-relative paths.
    Everything else -- ``--amend``, ``--no-verify``/``-n``, ``--fixup``,
    ``-c``/``-C``, ``--author``, any abbreviation such as ``--amen`` --
    means "not this form" and falls through to ``ask``.
    """
    args = _simple_git(command, "commit")
    if args is None:
        return False
    i = 0
    paths_only = False
    while i < len(args):
        text = args[i].text
        if paths_only or not text.startswith("-"):
            if not _repo_relative(text):
                return False
        elif text == "--":
            paths_only = True
        elif text in ("-m", "-am"):
            if i + 1 >= len(args):
                return False
            i += 1  # the message: any text, quoted or not
        elif text == "-F":
            if i + 1 >= len(args) or not _repo_relative(args[i + 1].text):
                return False
            i += 1
        elif text.startswith("--message="):
            pass
        elif text.startswith("--file="):
            if not _repo_relative(text.removeprefix("--file=")):
                return False
        elif text not in ("-a", "--all"):
            return False
        i += 1
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
# the gate
# --------------------------------------------------------------------------- #


@dataclass
class BridgeGate(TieredGate):
    """``TieredGate`` that tries :class:`ClassifiedRule` entries first, on the whole command.

    First classified match in table order wins; otherwise the stock
    ``TieredGate`` matching runs on the UNMODIFIED request text -- Claude
    ``Bash`` included, heredoc bodies and all. (An earlier revision hid
    "data" heredoc bodies from the patterns; review showed a commented-out
    ``cat`` or a heredoc fed to ``bash -c "$(...)"`` turned that into a way to
    hide executed commands, so it was removed. Claude is instead instructed
    to write file content with Write/Edit -- see ``_engines``.)

    Everything else -- the audit record, the prompt shown to a human,
    session-grant scoping -- is ``TieredGate``'s own.
    """

    def _match(self, request: ApprovalRequest) -> Rule | None:
        if isinstance(request.arguments.get("command"), str):
            for rule in self.rules:
                if isinstance(rule, ClassifiedRule) and rule.matches(request):
                    return rule
        return super()._match(request)


__all__ = [
    "CLASSIFIERS",
    "BridgeGate",
    "ClassifiedRule",
    "is_readonly_gh_query",
    "is_simple_git_add",
    "is_simple_git_commit",
    "unwrap_powershell",
]
