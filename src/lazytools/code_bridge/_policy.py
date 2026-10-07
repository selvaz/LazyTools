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
"""

from __future__ import annotations

from lazybridge.ext.approval import Rule

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
    # Session grant, not allow: a human reviews the FIRST add/commit of this
    # job, then the rest of the same run is auto-granted. Not allow, because
    # content these agents stage/commit can carry command substitution a
    # Bash gate can't see inside a non-compound segment (see TieredGate's
    # own module docstring) -- so the first look still matters.
    Rule("session", "Bash", "git add*"),
    Rule("session", "Bash", "git commit*"),
    # These sit ABOVE the "git push*" session rule so the narrower, more
    # dangerous forms win first: a plain "git push" approved once must not
    # also auto-grant "--force" or a branch-deleting refspec for the rest of
    # the run. See LazyCEO's own table for the enumeration history (fnmatch
    # cannot express "a safe push" directly, so these say what a
    # session-granted push may NOT contain).
    # A global option before the subcommand ("git -C repo push --force",
    # "git --git-dir=.git push origin main") hides the subcommand from every
    # "git push ..." pattern below, so any such form asks. Found by Codex review.
    Rule("ask", "Bash", "git -*"),
    Rule("ask", "Bash", "git push -*"),
    Rule("ask", "Bash", "git push * -*"),
    Rule("ask", "Bash", "git push*'-*"),
    Rule("ask", "Bash", 'git push*"-*'),
    Rule("ask", "Bash", "git push*\\-*"),
    Rule("ask", "Bash", "git push *:*"),
    Rule("ask", "Bash", "git push *+*"),
    # Pushing to the default branch is the same act as merging, by another
    # door -- always ask, never covered by a push session grant. Matched as
    # a token ANYWHERE in the push, not just the last argument: "git push
    # origin main feature" updates main too, and "* main"/"* master" alone
    # (matching only the final token) would miss it. Broader than strictly
    # necessary (a branch literally named "mainline" also asks) -- a false
    # "ask" is cheap; a missed push to main is not. Found by Codex review.
    Rule("ask", "Bash", "git push *main*"),
    Rule("ask", "Bash", "git push *master*"),
    # A push that names no branch ("git push", "git push origin", "git push
    # origin HEAD") goes wherever the checked-out branch goes -- main, if
    # that is what is checked out -- and the rules above cannot see it. Only
    # a push naming both remote and branch is session-grantable; every
    # implicit form asks. Found by Codex review.
    Rule("ask", "Bash", "git push *HEAD*"),
    Rule("session", "Bash", "git push * *"),
    Rule("ask", "Bash", "git push*"),
    Rule("session", "Bash", "gh pr create*"),
    # Merging, resetting history, and deleting are not session-grantable:
    # each one is asked every time.
    Rule("ask", "Bash", "gh pr merge*"),
    Rule("ask", "Bash", "git reset --hard*"),
    Rule("ask", "Bash", "*rm *"),
    Rule("ask", "Bash", "*delete*"),
    # Default-allow catch-all for Bash, NOT an allow-list of safe commands --
    # the specific ask/session patterns above already caught the dangerous
    # forms; anything else (tests, scripts, curl, ...) runs free, matching
    # how this bridge's own human operates at the terminal.
    Rule("allow", "Bash"),
    # Every command a coding engine escalates beyond its own sandbox reaches
    # a human. Codex's approval_policy="on-request" only fires when Codex
    # ITSELF decides it needs more than workspace-write, so a request
    # arriving here is already "I want to step outside what I was granted" --
    # exactly the moment for a person to decide, not a pattern match.
    Rule("ask", "*", kind_pattern="command"),
)

__all__ = ["CODING_RULES"]
