"""LazyStudio published-agent provider for the LazyTools MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from lazybridge import Tool


class StudioTools:
    """Discover and invoke tools published by a local LazyStudio instance."""

    _is_lazy_tool_provider = True

    def __init__(self, *, allow_run: bool = False) -> None:
        self.base_url = os.environ.get("LAZYSTUDIO_URL", "http://127.0.0.1:8000").rstrip("/")
        token_file = Path(
            os.path.expanduser(os.environ.get("LAZYSTUDIO_TOKEN_FILE", "~/.lazystudio/operator-token"))
        )
        self.token = ""
        self.problem = ""
        try:
            self.token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            self.problem = f"operator token not found at {token_file}"
        if not self.token and not self.problem:
            self.problem = f"operator token is empty at {token_file}"

        self.published: list[dict[str, Any]] = []
        if self.token:
            try:
                payload = self._request("GET", "/api/mcp-tools")
                if not isinstance(payload, list):
                    raise ValueError("tool list was not a JSON array")
                self.published = [item for item in payload if isinstance(item, dict)]
            except (OSError, ValueError) as exc:
                self.problem = f"could not connect to LazyStudio at {self.base_url}: {exc}"
        self.allow_run = allow_run

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            # Never include request headers (and therefore never the token) in errors.
            raise OSError(f"LazyStudio returned HTTP {exc.code}") from None
        except URLError as exc:
            raise OSError(str(exc.reason)) from None

    @staticmethod
    def _result(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise ValueError("LazyStudio returned an invalid result")
        status = payload.get("status")
        run_id = payload.get("run_id")
        if status == "running" and isinstance(run_id, str):
            return f"still running, run_id={run_id}"
        if status in {"succeeded", "failed"} and isinstance(payload.get("output"), str):
            return payload["output"]
        raise ValueError("LazyStudio returned an invalid result")

    def _call(self, name: str, task: str) -> str:
        payload = self._request(
            "POST", f"/api/mcp-tools/{quote(name, safe='')}/call", {"task": task}
        )
        return self._result(payload)

    def studio_run_status(self, run_id: str) -> str:
        """Get the latest result for a LazyStudio run."""
        if not self.token:
            return self._status_text()
        payload = self._request("GET", f"/api/mcp-tools/runs/{quote(run_id, safe='')}")
        return self._result(payload)

    def _status_text(self) -> str:
        detail = self.problem or "LazyStudio was unavailable when this MCP server started"
        return (
            f"Studio unavailable: {detail}. Start LazyStudio, and ensure LAZYSTUDIO_URL and "
            "LAZYSTUDIO_TOKEN_FILE point to it and its operator-token; then restart lazytools-mcp."
        )

    def studio_status(self) -> str:
        """Explain why LazyStudio tools are unavailable and how to enable them."""
        return self._status_text()

    def _published_tool(self, name: str, description: str) -> Tool:
        def run(task: str) -> str:
            return self._call(name, task)

        return Tool.wrap(run, name=name, description=description)

    def as_tools(self) -> list[Tool]:
        tools = [
            Tool.wrap(
                self.studio_run_status,
                name="studio_run_status",
                description="Get the output or current state of a LazyStudio run.",
            )
        ]
        if self.problem:
            tools.append(
                Tool.wrap(
                    self.studio_status,
                    name="studio_status",
                    description="Explain how to connect this MCP server to LazyStudio.",
                )
            )
            return tools
        if not self.allow_run:
            return tools

        for item in self.published:
            name = item.get("name")
            if not isinstance(name, str):
                continue
            description = item.get("description")
            tools.append(
                self._published_tool(
                    name,
                    description if isinstance(description, str) else "Run a published LazyStudio agent.",
                )
            )
        return tools
