"""Case-insensitive target allow-list."""

from __future__ import annotations

from collections.abc import Iterable


class Allowlist:
    """Case-insensitive, string-normalized target allow-list.

    ``None`` means "no allow-list configured" → permits everything. An empty
    iterable means "deny everything".
    """

    def __init__(self, allowed: Iterable[object] | None) -> None:
        self._allowed = None if allowed is None else {str(a).lower() for a in allowed}

    def permits(self, target: object) -> bool:
        return self._allowed is None or str(target).lower() in self._allowed
