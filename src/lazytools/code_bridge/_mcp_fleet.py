"""Read-only MCP views over the ``lazytools-code-bridge`` CLI."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date
from typing import Any

from lazytools.code_bridge._mcp import QUERY_TIMEOUT, CodeBridgeTools


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
        argv = ["route", "--tier", tier]
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
