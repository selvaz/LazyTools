"""One-shot, target-bound confirmation grants for dangerous actions."""

from __future__ import annotations

#: Sentinel target for a grant that is not bound to a specific recipient/chat.
_ANY = "*"


class ConfirmationGate:
    """One-shot, target-bound confirmation grants for dangerous actions.

    Not a sticky boolean: each grant authorizes exactly one action, so an
    approved single message can never silently authorize a flood. Grants are
    matched from most to least specific: a target+scope grant before a
    target-only one, then an any-target+scope grant before an any-target one. A
    scope-bound grant is never spendable when no scope (``None``) is supplied at
    consume time. No process-global mutable state — grants live on the instance.

    The gate is scope-agnostic: the caller decides what ``scope`` means (in
    LazyPulse it is the running task id, read from
    :func:`lazytools.safety.current_scope`) and passes it in. This is what keeps
    the safety layer free of any orchestration dependency.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        # Keys are ``(target, scope)`` where target is a lowercased string or
        # ``_ANY`` and scope is an opaque binding (the task id) or ``None``.
        self._grants: dict[tuple[str, str | None], int] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def grant(self, target: object, *, scope: str | None = None) -> None:
        """Authorize exactly one action to ``target`` (the tighter grant)."""
        self._add((str(target).lower(), scope))

    def grant_any(self, *, scope: str | None = None) -> None:
        """Authorize exactly one action to any target (subject to allow-list)."""
        self._add((_ANY, scope))

    def _add(self, key: tuple[str, str | None]) -> None:
        self._grants[key] = self._grants.get(key, 0) + 1

    def consume(self, target: object, *, scope: str | None = None) -> bool:
        """Spend one matching grant for ``target`` in ``scope``; ``True`` if found.

        Returns ``True`` immediately when the gate is disabled. A scope-bound
        grant is only matched when the same ``scope`` is supplied here.
        """
        if not self._enabled:
            return True
        target_l = str(target).lower()
        candidates: list[tuple[str, str | None]] = []
        if scope is not None:
            candidates.append((target_l, scope))
        candidates.append((target_l, None))
        if scope is not None:
            candidates.append((_ANY, scope))
        candidates.append((_ANY, None))
        for key in candidates:
            if self._grants.get(key, 0) > 0:
                self._grants[key] -= 1
                return True
        return False
