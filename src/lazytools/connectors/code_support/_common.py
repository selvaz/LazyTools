"""Per-call ``model`` / ``effort`` / ``session_name`` plumbing shared by every
engine-backed code tool (reviewers, consultants and the gated writers).

Kept free of imports from the sibling modules so all of them can use it.

What lives here, and why once:

* the **effort vocabularies**. ``ClaudeCodeEngine`` validates its own
  ``reasoning_effort`` (``low``/``medium``/``high``/``xhigh``/``max``), but
  ``CodexEngine`` passes the string through unvalidated — each Codex model
  advertises its own accepted values over ``model/list``. A typo such as
  ``"hgih"`` would otherwise travel all the way to the App Server and come back
  as an opaque turn failure minutes later, so both providers are checked here,
  up front, with an error that lists what is allowed. The Codex set is the
  *union* the CLI knows about; a particular model may accept fewer, in which
  case Codex itself still rejects the value at the turn. Nothing here lists
  *models*: those change faster than this package, and ``None`` always means
  "the provider's own default".
* the **session registry** — one helper, so the scope/registry rules are the
  same for the review tools, the writers and the management tools.
* the **session-name check**, which has to run *before* a write grant is
  consumed: a mistyped alias must not burn the human's one-shot approval.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lazybridge.engines.sessions import SessionRegistry

#: Reasoning efforts ``ClaudeCodeEngine`` accepts (mirrors the engine's own check).
CLAUDE_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: Reasoning efforts known to the Codex CLI, as a union across models.
CODEX_EFFORTS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def clean(value: str | None) -> str | None:
    """``None`` for an omitted argument *or* an empty one.

    MCP clients that fill in every parameter send ``""`` for "not given";
    treating that as a real (invalid) value would make the optional arguments
    unusable for them.
    """
    if value is None:
        return None
    value = value.strip()
    return value or None


def check_effort(effort: str | None, allowed: Iterable[str], *, provider: str) -> str | None:
    """Validate a per-call reasoning effort; ``None`` passes through unchanged."""
    effort = clean(effort)
    if effort is None:
        return None
    choices = tuple(allowed)
    if effort not in choices:
        raise ValueError(f"effort {effort!r} is not valid for {provider}; use one of: {', '.join(choices)}")
    return effort


def session_registry(registry: SessionRegistry | None = None) -> SessionRegistry:
    """The registry the code tools bind aliases in.

    ``registry`` (a provider/factory argument, there for tests and for callers
    that keep their aliases somewhere other than the default) wins; otherwise
    LazyBridge's process-wide default — ``$LAZYBRIDGE_SESSIONS_FILE`` or
    ``~/.lazybridge/sessions.json``. The *scope* of an alias is the resolved
    working directory of the call, which the engine derives from its ``cwd``.
    """
    if registry is not None:
        return registry
    from lazybridge.engines.sessions import default_session_registry

    return default_session_registry()


def check_session_name(kind: str, cwd: str | Path, name: str | None, registry: SessionRegistry) -> str | None:
    """Validate an alias up front and return it (``None`` if not given).

    Uses the registry's own public ``resolve`` so the rule is LazyBridge's, not
    a copy of it: a bad name raises ``ValueError`` saying what a name may
    contain. Any other trouble reading the registry is left for the engine,
    which degrades it to a warning instead of failing the call.
    """
    name = clean(name)
    if name is None:
        return None
    try:
        registry.resolve(kind, cwd, name)
    except ValueError:
        raise
    except Exception:  # an unreadable registry is the engine's warning to give
        pass
    return name
