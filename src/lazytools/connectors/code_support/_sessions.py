"""Management tools for the durable session names the code tools use.

Every engine-backed tool in this package (``codex_code_review``,
``codex_ask``, ``codex_review_changes``, ``claude_code_review``,
``claude_ask``, ``codex_write``, ``claude_code_write``) accepts an optional
``session_name``: a short alias for a durable Codex thread / Claude Code
session, kept in LazyBridge's ``SessionRegistry`` and scoped to the call's
working directory. :class:`CodeSessionTools` is the one place to look at and
curate those names:

* ``code_sessions_list`` -- read-only: what names exist, what they point at.
* ``code_sessions_bind`` / ``_rename`` / ``_forget`` -- mutators, exposed only
  when the provider is built with ``allow_mutate=True`` (the MCP server does
  that only under ``--allow-unsafe``).

Forgetting or renaming an alias never touches the native session itself, and
binding attaches a name to an id you already hold (e.g. a ``thread_id`` from a
review header) -- it never creates or resumes anything.

Every ``repo_path`` is confined to the configured root, the same rule the
review tools apply, and listing without one only shows scopes under that root.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazytools.connectors.code_support._claude_review import _build_root
from lazytools.connectors.code_support._common import clean
from lazytools.connectors.code_support._common import session_registry as _session_registry
from lazytools.connectors.code_support._review import _resolve_repo

if TYPE_CHECKING:
    from lazybridge import Tool
    from lazybridge.engines.sessions import SessionRegistry

_KINDS = ("codex", "claude")


def _kind(kind: str | None) -> str | None:
    kind = clean(kind)
    if kind is not None and kind not in _KINDS:
        raise ValueError(f"kind must be one of {', '.join(_KINDS)}; got {kind!r}")
    return kind


def _required_kind(kind: str | None) -> str:
    value = _kind(kind)
    if value is None:
        raise ValueError(f"kind is required: one of {', '.join(_KINDS)}")
    return value


class CodeSessionTools:
    """Tool provider for listing and curating code-tool session names.

    Parameters
    ----------
    root:
        Confinement root for every ``repo_path`` (default: ``$LAZYTOOLS_CODE_ROOT``,
        else the process' cwd) -- the same default the review tools use.
    allow_mutate:
        Expose ``code_sessions_bind`` / ``_rename`` / ``_forget`` as well as the
        read-only ``code_sessions_list``. Default ``False``.
    session_registry:
        The registry to manage; default is LazyBridge's (``$LAZYBRIDGE_SESSIONS_FILE``
        or ``~/.lazybridge/sessions.json``). Mainly for tests.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        root: str | None = None,
        allow_mutate: bool = False,
        session_registry: SessionRegistry | None = None,
    ) -> None:
        self._root = _build_root(root)
        self._allow_mutate = allow_mutate
        self._registry = session_registry

    @property
    def allow_mutate(self) -> bool:
        return self._allow_mutate

    def _reg(self) -> SessionRegistry:
        return _session_registry(self._registry)

    def _scope(self, repo_path: str | None) -> Path:
        return _resolve_repo(clean(repo_path), self._root)

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        from lazybridge import Tool

        tools = [
            Tool.wrap(
                self.code_sessions_list,
                name="code_sessions_list",
                description=(
                    "List the named Codex / Claude Code sessions the code tools keep (read-only). "
                    "Args: kind (str, optional -- 'codex' or 'claude'); repo_path (str, optional -- "
                    "only that repository's names; default: every repository under the code root). "
                    "Each row: name, kind, native_id (thread_id for codex, session_id for claude), "
                    "scope (the repository), model, effort, updated_at. Pass a name back as "
                    "session_name to a review / ask / write tool to continue that conversation."
                ),
            )
        ]
        if self._allow_mutate:
            tools += [
                Tool.wrap(
                    self.code_sessions_bind,
                    name="code_sessions_bind",
                    description=(
                        "Attach a session name to a native id you already hold (a thread_id / "
                        "session_id from a tool header), or re-point an existing name at another "
                        "one. Args: kind ('codex' or 'claude'); name (str: letters, digits, '_', "
                        "'.', '-', starting with a letter); native_id (str); repo_path (str, "
                        "optional). Does not create or resume a session."
                    ),
                ),
                Tool.wrap(
                    self.code_sessions_rename,
                    name="code_sessions_rename",
                    description=(
                        "Rename a session name. Fails if the old name is unknown or the new one "
                        "already exists. Args: kind; old (str); new (str); repo_path (str, optional)."
                    ),
                ),
                Tool.wrap(
                    self.code_sessions_forget,
                    name="code_sessions_forget",
                    description=(
                        "Drop a session name. The native session itself is untouched. "
                        "Args: kind; name (str); repo_path (str, optional)."
                    ),
                ),
            ]
        return tools

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #
    def code_sessions_list(self, kind: str | None = None, repo_path: str | None = None) -> list[dict[str, Any]]:
        """Named sessions (``name``, ``kind``, ``native_id``, ``scope``, ``model``, ``effort``, ``updated_at``)."""
        from lazybridge.engines.sessions import normalize_scope

        wanted = _kind(kind)
        reg = self._reg()
        if clean(repo_path):
            rows = reg.entries(wanted, self._scope(repo_path))
        else:
            root = normalize_scope(self._root)
            prefix = root.rstrip("/") + "/"
            rows = [r for r in reg.entries(wanted) if r["scope"] == root or r["scope"].startswith(prefix)]
        keys = ("name", "kind", "native_id", "scope", "model", "effort", "updated_at")
        return [{k: row.get(k) for k in keys} for row in rows]

    def code_sessions_bind(self, kind: str, name: str, native_id: str, repo_path: str | None = None) -> dict[str, Any]:
        """Bind ``name`` to ``native_id`` for ``kind`` in ``repo_path``'s scope."""
        self._require_mutate()
        k = _required_kind(kind)
        scope = self._scope(repo_path)
        self._reg().bind(k, scope, name, native_id)
        return {"bound": True, "kind": k, "name": name, "native_id": native_id, "scope": str(scope)}

    def code_sessions_rename(self, kind: str, old: str, new: str, repo_path: str | None = None) -> dict[str, Any]:
        """Rename a session name."""
        self._require_mutate()
        k = _required_kind(kind)
        scope = self._scope(repo_path)
        try:
            self._reg().rename(k, scope, old, new)
        except KeyError as exc:
            raise ValueError(str(exc.args[0]) if exc.args else str(exc)) from exc
        return {"renamed": True, "kind": k, "old": old, "new": new, "scope": str(scope)}

    def code_sessions_forget(self, kind: str, name: str, repo_path: str | None = None) -> dict[str, Any]:
        """Drop a session name (the native session is untouched)."""
        self._require_mutate()
        k = _required_kind(kind)
        scope = self._scope(repo_path)
        removed = self._reg().forget(k, scope, name)
        return {"forgotten": removed, "kind": k, "name": name, "scope": str(scope)}

    def _require_mutate(self) -> None:
        # as_tools() does not even expose the mutators without allow_mutate;
        # this keeps a direct method call from being a way around that.
        if not self._allow_mutate:
            raise PermissionError(
                "session names are read-only here: build CodeSessionTools(allow_mutate=True) "
                "(the MCP server does so under --allow-unsafe)"
            )
