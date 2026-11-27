"""Read-only MCP views over the ``lazytools-code-bridge`` CLI."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import date
from typing import Any

from lazytools.code_bridge._mcp import QUERY_TIMEOUT, CodeBridgeTools

_EVENTS_MAX_WAIT = 60
_EVENTS_POLL_INTERVAL = 0.5


class FleetTools(CodeBridgeTools):
    """Read-only fleet, routing, model, and capability views."""

    _is_lazy_tool_provider = True

    @staticmethod
    def _safe_text(name: str, value: str) -> str:
        if not isinstance(value, str) or value.startswith("-"):
            raise ValueError(f"{name} must be text and may not start with '-'")
        return value

    @classmethod
    def _date(cls, name: str, value: str) -> str:
        value = cls._safe_text(name, value)
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must use YYYY-MM-DD format") from exc
        if parsed.isoformat() != value:
            raise ValueError(f"{name} must use YYYY-MM-DD format")
        return value

    def _read_call(self, argv: list[str]) -> Any:
        """Invoke a read-only CLI command and decode its JSON output."""
        try:
            code, out, err = self._invoke(argv, QUERY_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"the bridge command did not finish within {QUERY_TIMEOUT:g}s"}

        payloads: list[Any] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                payloads.append(json.loads(line))
            except ValueError:
                continue
        if payloads:
            payload: Any = payloads[0] if len(payloads) == 1 else payloads
            if code == 0:
                return payload
            return {
                "ok": False,
                "exit_code": code,
                "result": payload,
                **({"stderr": err.strip()[:2000]} if err.strip() else {}),
            }
        if code == 0 and not out.strip():
            return []  # e.g. `jobs` prints nothing when no job matches: an empty fleet, not an error
        return {
            "ok": False,
            "exit_code": code,
            "error": (err or out).strip()[:2000] or f"exit code {code}",
        }

    def bridge_jobs(self, all: bool = False) -> Any:
        """List active bridge jobs, or include finished jobs with ``all``."""
        if not isinstance(all, bool):
            raise ValueError("all must be a boolean")
        return self._read_call(["jobs", *(["--all"] if all else []), "--json"])

    def bridge_pending(self) -> Any:
        """List open code-bridge job approval tickets."""
        return self._read_call(["pending", "--json"])

    def bridge_autonomy(
        self, week: str | None = None, since: str | None = None, until: str | None = None
    ) -> Any:
        """Report autonomous completion over an ISO week or UTC date range."""
        if week is not None:
            week = self._safe_text("week", week)
            if not re.fullmatch(r"\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])", week):
                raise ValueError("week must use YYYY-Www format")
            if since is not None or until is not None:
                raise ValueError("week cannot be combined with since or until")
        argv = ["autonomy"]
        if week is not None:
            argv += ["--week", week]
        if since is not None:
            argv += ["--since", self._date("since", since)]
        if until is not None:
            argv += ["--until", self._date("until", until)]
        return self._read_call([*argv, "--json"])

    def bridge_route(
        self, tier: str, engine: str | None = None, where: str | None = None, needs: str | None = None
    ) -> Any:
        """Explain the bridge's quota-aware route without launching a job."""
        from lazytools.code_bridge import _cloud_place, _jobs

        choices = {"basic", "writing", "thinking", "critical"}
        tier = self._safe_text("tier", tier)
        if tier not in choices:
            raise ValueError(f"tier must be one of: {', '.join(sorted(choices))}")
        # Describe the configured repository, not the server process's working directory.
        argv = ["route", "--tier", tier, "--cwd", self._root, "--root", self._root]
        for name, value, allowed in (
            ("engine", engine, set(_jobs.ENGINES)),
            ("where", where, set(_cloud_place.WHERE_CHOICES)),
            ("needs", needs, {"images"}),
        ):
            if value is None:
                continue
            value = self._safe_text(name, value)
            if value not in allowed:
                raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
            argv += [f"--{name}", value]
        return self._read_call([*argv, "--json"])

    def bridge_models(self) -> Any:
        """Inspect model availability without running the optional Claude probe."""
        return self._read_call(["models", "--json"])

    def bridge_events(self, cursor: str = "", timeout_s: int = 0) -> Any:
        """Return bridge job events since ``cursor`` as ``{"events": [...], "cursor": "..."}``.

        Pass the returned cursor to the next call. An empty cursor starts "from now": jobs
        that already finished are not replayed, open approval tickets are reported. An event
        that happens between two calls is returned by the next call. With ``timeout_s`` (at
        most 60) the call waits that long for at least one event. Event kinds: ``approval``,
        ``done``, ``failed``, ``interrupted``, ``cancelled``, ``landed``. A cursor this tool
        did not return is refused.
        """
        from lazytools.code_bridge import _events

        if not isinstance(cursor, str):
            raise ValueError("cursor must be text")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or not 0 <= timeout_s <= _EVENTS_MAX_WAIT:
            raise ValueError(f"timeout_s must be an integer from 0 to {_EVENTS_MAX_WAIT}")
        if cursor:
            _events.decode_cursor(cursor)  # refuse a malformed cursor before reading any store
        deadline = time.monotonic() + timeout_s
        while True:
            events, cursor = _events.poll(cursor)
            remaining = deadline - time.monotonic()
            if events or remaining <= 0:
                return {"events": events, "cursor": cursor}
            time.sleep(min(_EVENTS_POLL_INTERVAL, remaining))

    def capabilities_search(self, query: str = "") -> Any:
        """Search the capabilities catalog and return its tabular rows."""
        query = self._safe_text("query", query)
        argv = ["capabilities", *([query] if query else [])]
        try:
            code, out, err = self._invoke(argv, QUERY_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"the bridge command did not finish within {QUERY_TIMEOUT:g}s"}
        if code != 0:
            return {
                "ok": False,
                "exit_code": code,
                "error": (err or out).strip()[:2000] or f"exit code {code}",
            }
        rows = []
        for line in out.splitlines():
            cells = line.split("\t")
            if len(cells) == 4:
                rows.append(dict(zip(("section", "capability", "call", "status"), cells, strict=True)))
        return rows

    def as_tools(self) -> list[Any]:
        """Only the read-only views; the inherited submit/status/result stay in ``code_bridge``.

        These are fleet-wide (every job and ticket in the shared store), so they carry no
        ``CODE_ACCESS`` grant: a task-scoped server (``_scoped_tools``) never exposes them, and
        each call refuses inside an authenticated task context as a second guard.
        """
        from lazybridge import Tool

        methods = (self.bridge_jobs, self.bridge_pending, self.bridge_route, self.bridge_autonomy,
                   self.bridge_models, self.capabilities_search, self.bridge_events)
        return [Tool.wrap(_unscoped(method), name=method.__name__) for method in methods]


def _unscoped(method: Any) -> Any:
    from functools import wraps

    from lazytools.connectors.code_support._policy import current_task

    @wraps(method)
    def call(*args: Any, **kwargs: Any) -> Any:
        if current_task() is not None:
            raise PermissionError("fleet-wide views are not available inside a task-scoped context")
        return method(*args, **kwargs)

    return call


__all__ = ["FleetTools"]
