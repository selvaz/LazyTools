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
command for Codex escalations. Claude Bash uses its own whole-command,
fail-closed capability check. ``TieredGate``
splits on ``|`` and newlines, which is right for its fnmatch rules but would
cut a quoted ``--jq '... | ...'`` filter in half; the classifier does its own
quote-aware tokenisation instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from fnmatch import fnmatch
from typing import Any, cast

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

#: Refused inside any argument AFTER quote removal: every quote and every
#: escape/substitution character. ``$``, backtick and ``\`` are already
#: refused anywhere by ``_FORBIDDEN_ANYWHERE``; listed again so this one rule
#: states the whole contract on its own.
_FAIL_CLOSED_CHARS = re.compile(r"""["'`\\$]""")


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
    for any empty argument, and for any argument that still contains a quote
    or escape character after quote removal (see the comments at the end).
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
    # Same reasoning, round 3: Windows PowerShell 5.1 re-quotes each argument
    # for the native command line WITHOUT escaping embedded double quotes, so
    # a single-quoted value holding a `"` is split again by the program:
    # `gh api --jq '". -XDELETE -q" .' repos/...` reaches gh as extra
    # arguments `-XDELETE -q ...` (a DELETE), and `git commit -m '"x --amend -m" y'`
    # as `--amend`. After three rounds on PowerShell quoting the rule is
    # fail-closed, not a model of it: no argument, after quote removal, may
    # contain ANY quote or escape/substitution character. A jq filter that
    # needs a string literal ("...") therefore asks; documented trade-off.
    if any(_FAIL_CLOSED_CHARS.search(word.text) for word in words):
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

#: No --jq/-q/--template/-t anywhere: a jq filter can print the process environment
#: (``--jq env``) and a Go template can reach more, leaking API keys and bot tokens.
_OUTPUT_FLAGS = frozenset({"--json"})
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
        frozenset({"--cache"}),
        frozenset({"--paginate", "--slurp", "--include", "-i", "--silent"}),
        1,
    ),
}  # fmt: skip

#: The shapes the Codex escalation classifier auto-allows. ``_GH_READONLY`` itself
#: is also the base of routine-auto's own validated gh shapes (``_routine.py``,
#: which keeps ``--watch``); the unattended classifier must not block the job by
#: polling, so ``gh pr checks`` loses ``--watch`` and ``--interval`` here only.
_GH_ESCALATION_READONLY: dict[tuple[str, ...], _GhShape] = {
    **_GH_READONLY,
    ("pr", "checks"): _GhShape(_OUTPUT_FLAGS | _REPO_FLAGS, frozenset({"--required", "--fail-fast"}), 1),
}

_API_ENDPOINT = re.compile(r"/?repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?")


def _api_endpoint_ok(endpoint: str) -> bool:
    return bool(_API_ENDPOINT.fullmatch(endpoint)) and ".." not in endpoint.split("/")


def is_readonly_gh_query(command: str, *, shapes: dict[tuple[str, ...], _GhShape] | None = None) -> bool:
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
    for prefix, candidate in (shapes or _GH_ESCALATION_READONLY).items():
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


# --------------------------------------------------------------------------- #
# Claude Bash session grants: push and PR creation
# --------------------------------------------------------------------------- #

#: A ref the push grant can cover. Matched against the UNQUOTED token text, so
#: ``ma"i"n`` / ``HE''AD`` (which the raw-text ``*main*`` / ``*HEAD*`` ask
#: patterns miss, and which the tokeniser would otherwise reassemble into
#: ``main`` / ``HEAD``) never reach a grant.
_SAFE_REF = re.compile(r"[A-Za-z0-9_./][A-Za-z0-9_./-]*")
_PROTECTED_REF = re.compile(r"main|master|head", re.IGNORECASE)


def short_branch(ref: str | None) -> str | None:
    """``refs/heads/feat/x`` -> ``feat/x``; ``None`` for a detached HEAD or any other ref."""
    if ref and ref.startswith("refs/heads/") and _SAFE_REF.fullmatch(ref[len("refs/heads/") :]):
        return ref[len("refs/heads/") :]
    return None


def push_grantable(words: list[_Word], job_branch: str | None) -> bool:
    """``git push origin <job branch>`` and nothing else, every word unquoted.

    Only the remote ``origin`` is grantable (not ``../other``, ``//host/x`` or
    an arbitrary name), with exactly one refspec equal to this job's branch:
    no ``:``/``+`` refspecs, no flags, no extra refs. With no known job branch
    (detached HEAD, not a repository) nothing is grantable.
    """
    if not job_branch or not _SAFE_REF.fullmatch(job_branch) or _PROTECTED_REF.search(job_branch):
        return False
    if len(words) != 4 or any(word.quoted for word in words):
        return False
    return [word.text for word in words] == ["git", "push", "origin", job_branch]


_PR_VALUE_FLAGS = frozenset({"--title", "-t", "--body", "-b", "--base", "-B", "--head", "-H", "--label", "-l"})
_PR_HEAD_FLAGS = frozenset({"--head", "-H"})
_PR_BOOL_FLAGS = frozenset({"--draft", "-d", "--fill"})


def pr_create_grantable(words: list[_Word], job_branch: str | None) -> bool:
    """``gh pr create`` with only title/body (inline text)/base/head/label/draft/fill.

    Anything else asks: ``--body-file``/``-F`` (reads a local file into a public PR
    body), ``--template``, ``--repo``/``-R``, ``--web``, ``--hostname``, ``--fill-*``,
    ``--recover``, positionals. ``--head`` must be this job's branch. Flag values
    may not start with ``-`` (a value could otherwise smuggle in a second flag).
    """
    if len(words) < 3 or any(word.quoted for word in words[:3]) or [w.text for w in words[:3]] != ["gh", "pr", "create"]:
        return False
    rest = words[3:]
    i = 0
    while i < len(rest):
        word = rest[i]
        name, equals, inline = word.text.partition("=")
        if not name.startswith("-") or (word.quoted and word.text.startswith("-")):
            return False  # positional, or a quoted flag name
        if name in _PR_VALUE_FLAGS:
            if equals and not name.startswith("--"):
                return False
            if equals:
                value = inline
            else:
                i += 1
                if i >= len(rest):
                    return False
                value = rest[i].text
            if not value or value.startswith("-"):
                return False
            if name in _PR_HEAD_FLAGS and (not job_branch or value != job_branch):
                return False
        elif name not in _PR_BOOL_FLAGS or equals:
            return False
        i += 1
    return True


#: Tools that change a file and the argument(s) that name it.
_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_EDIT_PATH_KEYS = ("file_path", "notebook_path", "path")


def git_control_path(path: str) -> bool:
    """True when ``path`` is under ``.git/`` or is a git config-bearing file.

    ``.git/config`` (``core.fsmonitor``, ``core.sshCommand``, remotes using
    ``ext::``), ``.git/hooks/**``, ``.gitattributes`` (``textconv`` / ``filter`` /
    external-diff drivers), ``.gitmodules`` and ``.gitconfig`` all make an
    allow-listed git read execute a command, so a write to one is never
    unattended. NTFS aliases are normalised: trailing dots/spaces, alternate data
    streams (``.git::$INDEX_ALLOCATION``) and 8.3 short names (``GIT~1``).
    """
    for segment in re.split(r"[/\\]", path):
        name = segment.split(":", 1)[0].rstrip(". ").lower()
        if name in (".git", ".gitattributes", ".gitmodules", ".gitconfig") or re.match(r"git[a-z0-9_]*~[0-9]", name):
            return True
    return False


_SHELL_RC = frozenset({
    ".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zprofile", ".zshenv",
    "microsoft.powershell_profile.ps1", "profile.ps1",
})


def _slashed(path: str) -> str:
    """Forward slashes, lower case, MSYS ``/c/x`` -> ``c:/x``, ``.`` and empty segments dropped."""
    text = re.sub(r"^/([A-Za-z])(?=/|$)", r"\1:", path.replace("\\", "/")).lower()
    return "/".join(part for part in text.split("/") if part not in ("", "."))


def outside_roots(path: str, roots: tuple[str, ...]) -> bool:
    """True unless ``path`` is relative without ``..``, or lies lexically inside one of ``roots``.

    Claude's file tools usually pass absolute paths, so an absolute path is accepted
    only under the job's cwd (or its own temp dir). Symlinks are not resolved.
    """
    parts = [part for part in re.split(r"[/\\]", path) if part]
    if ".." in parts:
        return True
    if not (path.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", path)):
        return False
    target = _slashed(path)
    return not any(
        root_text and (target == root_text or target.startswith(root_text + "/"))
        for root_text in (_slashed(root) for root in roots if root)
    )


def protected_write_path(path: str, roots: tuple[str, ...]) -> bool:
    """A Write/Edit target that always asks: outside the job, git control, agent/CI/shell config.

    Beyond :func:`git_control_path` and :func:`outside_roots`: ``.claude/**`` (hooks,
    settings, permissions), ``.mcp.json`` (spawns MCP servers), ``.husky/**`` (git
    hooks), ``.env*``, ``.vscode/settings.json`` and shell start-up files. Segments
    are normalised like :func:`git_control_path` (case, trailing dots/spaces, streams).
    """
    if git_control_path(path) or outside_roots(path, roots):
        return True
    names = [segment.split(":", 1)[0].rstrip(". ").lower() for segment in re.split(r"[/\\]", path) if segment]
    return (
        any(name in (".claude", ".husky", ".mcp.json") or name.startswith(".env") or name in _SHELL_RC for name in names)
        or (len(names) >= 2 and names[-2] == ".vscode" and names[-1] == "settings.json")
    )


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

    Routine-auto validators run first. Claude Bash then checks session rules
    and the read-only allow-list on the whole command, asking on any miss.
    Codex escalations try classified rules before stock ``TieredGate`` matching
    on the unmodified text. (An earlier revision hid
    "data" heredoc bodies from the patterns; review showed a commented-out
    ``cat`` or a heredoc fed to ``bash -c "$(...)"`` turned that into a way to
    hide executed commands, so it was removed. Claude is instead instructed
    to write file content with Write/Edit -- see ``_engines``.)

    Everything else -- the audit record, the prompt shown to a human,
    session-grant scoping -- is ``TieredGate``'s own.
    """

    routine: Any = None
    accepting: bool = True
    #: The branch this job started on (``feat/x``, not ``refs/heads/feat/x``),
    #: set by the job runner. The only ref a push / ``gh pr create --head`` grant
    #: covers; ``None`` (unknown, detached HEAD) means those always ask.
    job_branch: str | None = None
    #: The job's cwd and its own temp dir: the only roots Write/Edit may reach
    #: unattended (set by the job runner). Unknown roots make every absolute path ask.
    cwd: str | None = None
    temp_dir: str | None = None

    async def _decide(self, request: ApprovalRequest, rule: Rule | None) -> Any:
        if isinstance(rule, _RoutineMatch):
            return self.routine.approve(request, rule.rule_id), "routine-auto"
        if request.kind == "tool" and request.name == "Bash" and rule is not None:
            label = (
                f"matched {rule.tier} rule: {rule.arg_pattern}"
                if rule.arg_pattern else "no allow rule matched (fail closed)"
            )
            arguments = {key: value for key, value in request.arguments.items() if key != "gate_rule"}
            request = replace(request, arguments={"gate_rule": label, **arguments})
        return await super()._decide(request, rule)

    def _match(self, request: ApprovalRequest) -> Rule | None:
        if not self.accepting:
            return Rule("deny", "*")
        command = request.arguments.get("command")
        if self.routine is not None and isinstance(command, str) and (request.kind == "command" or request.name == "Bash"):
            rule_id = self.routine.match(request)
            if rule_id:
                return _RoutineMatch(cast(Any, "routine-auto"), "*", rule_id=rule_id)
            if self.routine.policy.enabled:
                from lazytools.code_bridge._routine import normalise

                normal = normalise(command)
                # Escalations rejected by a capability validator must not
                # earn an older classifier's allow/session grant. Apply the
                # same rule to protected Claude Bash command families.
                protected = normal is None or normal[1][0] in ("git", "gh", self.routine.policy.python_executable)
                if request.kind == "command" or protected:
                    return Rule("ask", "*")
        if request.kind == "tool" and request.name == "Bash":
            return self._match_bash(request)
        if isinstance(request.arguments.get("command"), str):
            for rule in self.rules:
                if isinstance(rule, ClassifiedRule) and rule.matches(request):
                    return rule
        matched = super()._match(request)
        if request.kind == "tool" and request.name in _EDIT_TOOLS and matched is not None and matched.tier == "allow":
            # A write into git's control files turns later allow-listed git reads
            # (git status -> core.fsmonitor, git diff -> textconv, ...) into code
            # execution, so it asks. A missing/odd path argument fails closed too.
            paths = [request.arguments[k] for k in _EDIT_PATH_KEYS if k in request.arguments]
            roots = tuple(root for root in (self.cwd, self.temp_dir) if root)
            if not paths or any(not isinstance(p, str) or not p or protected_write_path(p, roots) for p in paths):
                return Rule("ask", request.name)
        return matched

    def _match_bash(self, request: ApprovalRequest) -> Rule:
        from lazytools.code_bridge import _readonly

        command = request.arguments.get("command")
        if not isinstance(command, str):
            return Rule("ask", "Bash")
        stages = _readonly.pipeline(command)
        # Session grants cover a single command, with no redirects, wrappers,
        # expansion or shell operators. Never classify split shell segments.
        simple = stages is not None and len(stages) == 1 and stages[0] == command.strip()
        words = _tokenize(command) if simple else None
        for rule in self.rules:
            if isinstance(rule, ClassifiedRule):
                continue
            if rule.name_pattern != "Bash" or not rule.matches(request):
                continue
            if rule.tier == "session":
                if not words:
                    continue
                if rule.arg_pattern == "git add*" and not is_simple_git_add(command):
                    continue
                if rule.arg_pattern == "git commit*" and not is_simple_git_commit(command):
                    continue
                if rule.arg_pattern == "git push * *" and not push_grantable(words, self.job_branch):
                    continue
                if rule.arg_pattern == "gh pr create*" and not pr_create_grantable(words, self.job_branch):
                    continue
            if rule.tier == "ask" and rule.arg_pattern is None:
                rule_id = _readonly.match(command)
                if rule_id:
                    return Rule("allow", "Bash", arg_pattern=f"readonly:{rule_id}")
            return rule
        return Rule("ask", "Bash")


@dataclass(frozen=True)
class _RoutineMatch(Rule):
    rule_id: str = ""


__all__ = [
    "CLASSIFIERS",
    "BridgeGate",
    "ClassifiedRule",
    "is_readonly_gh_query",
    "is_simple_git_add",
    "is_simple_git_commit",
    "unwrap_powershell",
]
