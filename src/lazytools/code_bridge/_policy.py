"""The ``TieredGate`` rule table for the async code bridge.

Mirrors the generic coding-agent rules in LazyCEO's
``lazyceo.simple.agent.SIMPLE_RULES`` (``origin/main:src/lazyceo/simple/
agent.py``) -- the Read/Write/Edit/Bash/git/gh tiers every one of its
delegated coding sub-agents is held to. Deliberately leaves out LazyCEO's
own custom tool rules (blackboard verbs, specialist lifecycle, calendar,
...): this bridge only ever runs ONE coding engine per job, with no custom
tool surface of its own to gate.

First matching rule wins (see ``lazybridge.ext.approval.tiered.TieredGate``);
order matters. The specific ``Bash``/git/gh patterns must come before the
catch-all ``Rule("allow", "Bash")``, and the generic command escalation rule
stays last.

The table is meant for :class:`~lazytools.code_bridge._approvals.BridgeGate`,
which evaluates the :class:`ClassifiedRule` entries on the whole command
(Claude ``Bash`` patterns also see normalized segments, with the original
text and heredoc bodies retained). Under a plain
``TieredGate`` it stays safe -- a ``ClassifiedRule`` never matches a split
segment, so those requests simply ask.
"""

from __future__ import annotations

from lazybridge.ext.approval import Rule

from lazytools.code_bridge._approvals import BashGuardRule, BashRule, ClassifiedRule

#: Pure reads/navigation/web run free. Write/Edit are "allow" here because
#: this gate is the ONLY thing standing between the agent and a file change
#: (both engines are configured with nothing pre-approved -- see
#: ``_engines.py``): denying them here would make the writer unusable, and
#: a git checkout is the recovery rail, same reasoning as LazyCEO's table.
CODING_RULES: tuple[Rule, ...] = (
    Rule("allow", "Read"),
    Rule("allow", "Glob"),
    Rule("allow", "Grep"),
    Rule("allow", "Write"),
    Rule("allow", "Edit"),
    Rule("allow", "WebSearch"),
    Rule("allow", "WebFetch"),
    # Opaque interpreter payloads and truncating redirects always ask, even
    # after a session grant. Normalization is Claude-only; Codex classifiers
    # below keep their existing strict whole-command behavior.
    BashGuardRule("ask", "Bash", kind_pattern="tool", provider_pattern="claude-code"),
    # Session grant, not allow: a human reviews the FIRST add/commit of this
    # job, then the rest of the same run is auto-granted. Not allow, because
    # content these agents stage/commit can carry command substitution a
    # Bash gate can't see inside a non-compound segment (see TieredGate's
    # own module docstring) -- so the first look still matters.
    BashRule("session", "Bash", "git add*"),
    BashRule("session", "Bash", "git commit*"),
    # These sit ABOVE the "git push*" session rule so the narrower, more
    # dangerous forms win first: a plain "git push" approved once must not
    # also auto-grant "--force" or a branch-deleting refspec for the rest of
    # the run. See LazyCEO's own table for the enumeration history (fnmatch
    # cannot express "a safe push" directly, so these say what a
    # session-granted push may NOT contain).
    # A global option before the subcommand ("git -C repo push --force",
    # "git --git-dir=.git push origin main") hides the subcommand from every
    # "git push ..." pattern below, so any such form asks. Found by Codex review.
    BashRule("ask", "Bash", "git -*"),
    BashRule("ask", "Bash", "git push -*"),
    BashRule("ask", "Bash", "git push * -*"),
    BashRule("ask", "Bash", "git push*'-*"),
    BashRule("ask", "Bash", 'git push*"-*'),
    BashRule("ask", "Bash", "git push*\\-*"),
    BashRule("ask", "Bash", "git push *:*"),
    BashRule("ask", "Bash", "git push *+*"),
    # Pushing to the default branch is the same act as merging, by another
    # door -- always ask, never covered by a push session grant. Matched as
    # a token ANYWHERE in the push, not just the last argument: "git push
    # origin main feature" updates main too, and "* main"/"* master" alone
    # (matching only the final token) would miss it. Broader than strictly
    # necessary (a branch literally named "mainline" also asks) -- a false
    # "ask" is cheap; a missed push to main is not. Found by Codex review.
    BashRule("ask", "Bash", "git push *main*"),
    BashRule("ask", "Bash", "git push *master*"),
    # A push that names no branch ("git push", "git push origin", "git push
    # origin HEAD") goes wherever the checked-out branch goes -- main, if
    # that is what is checked out -- and the rules above cannot see it. Only
    # a push naming both remote and branch is session-grantable; every
    # implicit form asks. Found by Codex review.
    BashRule("ask", "Bash", "git push *HEAD*"),
    BashRule("session", "Bash", "git push * *"),
    BashRule("ask", "Bash", "git push*"),
    BashRule("session", "Bash", "gh pr create*"),
    # Merging, resetting history, and deleting are not session-grantable:
    # each one is asked every time.
    BashRule("ask", "Bash", "gh pr merge*"),
    BashRule("ask", "Bash", "git reset --hard*"),
    BashRule("ask", "Bash", "git branch *-D*"),
    BashRule("ask", "Bash", "git branch *-d*"),
    BashRule("ask", "Bash", "git clean*"),
    BashRule("ask", "Bash", "git checkout --*"),
    BashRule("ask", "Bash", "git checkout .*"),
    # `git checkout <ref> -- <paths>` overwrites working-tree files as surely as
    # `git checkout -- <paths>`; switching branches (`git checkout feature`) stays allowed.
    BashRule("ask", "Bash", "git checkout * -- *"),
    BashRule("ask", "Bash", "git checkout * ."),
    BashRule("ask", "Bash", "git restore*"),
    BashRule("ask", "Bash", "git stash drop*"),
    BashRule("ask", "Bash", "git stash clear*"),
    BashRule("ask", "Bash", "gh pr close*"),
    BashRule("ask", "Bash", "gh * delete*"),
    BashRule("ask", "Bash", "remove-item*"),
    BashRule("ask", "Bash", "del *"),
    BashRule("ask", "Bash", "rmdir*"),
    BashRule("ask", "Bash", "rd *"),
    BashRule("ask", "Bash", "truncate*"),
    BashRule("ask", "Bash", "*rm *"),
    BashRule("ask", "Bash", "*delete*"),
    # Default-allow catch-all for Bash, NOT an allow-list of safe commands --
    # recognized wrappers, redirects and destructive forms ask above.
    # Unlisted commands (tests, scripts, curl, ...) run free; this is a
    # heuristic policy, not a shell sandbox or a guarantee about script code.
    Rule("allow", "Bash"),
    # --- Sandbox escalations (kind="command", Codex's "codex-shell") ------
    # Decided by explicit, unit-tested classifiers over the WHOLE command
    # (see _approvals.py), never by fnmatch: each is an allow-list over a
    # strict tokenisation, so anything compound, redirected, substituted or
    # simply unrecognised falls through to the catch-all "ask" below.
    #
    # A read-only GitHub query (gh pr view|checks|diff, gh run view|list,
    # gh api GET on repos/...). Codex escalates these only because its
    # sandbox has no network; they read, never write. Network stays OFF in
    # the sandbox, so every gh/git WRITE still needs an escalation -- and
    # still reaches the catch-all ask.
    ClassifiedRule("allow", "*", kind_pattern="command", classifier="readonly_gh_query"),
    # The same session semantics the Bash rules above give git add/commit,
    # for the escalated form: the first one asks, the rest of this run is
    # granted -- but only for ONE simple git add / git commit (no chaining,
    # no `git -C`, no --amend/--no-verify). Escalated pushes are NOT here:
    # they keep asking every time via the catch-all.
    ClassifiedRule("session", "*", kind_pattern="command", classifier="simple_git_add"),
    ClassifiedRule("session", "*", kind_pattern="command", classifier="simple_git_commit"),
    # Every other command a coding engine escalates beyond its own sandbox
    # reaches a human. Codex's approval_policy="on-request" only fires when
    # Codex ITSELF decides it needs more than workspace-write, so a request
    # arriving here is already "I want to step outside what I was granted" --
    # exactly the moment for a person to decide, not a pattern match.
    Rule("ask", "*", kind_pattern="command"),
)

__all__ = ["CODING_RULES"]
