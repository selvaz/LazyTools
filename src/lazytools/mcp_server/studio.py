"""LazyStudio published-agent provider for the LazyTools MCP server."""

from __future__ import annotations

import json
import logging
import os
import re
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from lazybridge import Tool

logger = logging.getLogger(__name__)
_PUBLISHED_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED_NAMES = {"studio_run_status", "studio_status"}
CALLER_TOKEN_ENV = "LAZYSTUDIO_CALLER_TOKEN"
CALLER_TOKEN_HEADER = "X-LazyStudio-Caller-Token"
_CALLER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _caller_token() -> str | None:
    """The secret capability of the LazyStudio run that launched this server, if any."""
    value = os.environ.get(CALLER_TOKEN_ENV, "")
    return value if _CALLER_TOKEN_RE.fullmatch(value) else None


class StudioTools:
    """Discover and invoke tools published by a local LazyStudio instance."""

    _is_lazy_tool_provider = True

    def __init__(self, *, allow_run: bool = False) -> None:
        self.base_url = os.environ.get("LAZYSTUDIO_URL", "http://127.0.0.1:8000").rstrip("/")
        self.wait_seconds = self._wait_seconds()
        token_file = Path(os.path.expanduser(os.environ.get("LAZYSTUDIO_TOKEN_FILE", "~/.lazystudio/operator-token")))
        self.token = ""
        self.problem = ""
        try:
            self.token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            self.problem = f"operator token not found at {token_file}"
        if not self.token and not self.problem:
            self.problem = f"operator token is empty at {token_file}"
        if self.token and not self._token_destination_allowed():
            self.problem = (
                "refusing to send the operator token to a non-loopback URL; set LAZYSTUDIO_ALLOW_REMOTE=1 to allow it"
            )

        self.published: list[dict[str, Any]] = []
        if self.token and not self.problem:
            try:
                payload = self._request("GET", "/api/mcp-tools")
                if not isinstance(payload, list):
                    raise ValueError("tool list was not a JSON array")
                self.published = [item for item in payload if isinstance(item, dict)]
            except (OSError, ValueError) as exc:
                self.problem = f"could not connect to LazyStudio at {self.base_url}: {exc}"
        self.allow_run = allow_run

    @staticmethod
    def _wait_seconds() -> int:
        raw = os.environ.get("LAZYSTUDIO_WAIT_SECONDS", "60")
        try:
            value = int(raw)
        except ValueError:
            logger.warning("Invalid LAZYSTUDIO_WAIT_SECONDS=%r; using 60", raw)
            return 60
        return min(600, max(0, value))

    def _token_destination_allowed(self) -> bool:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if os.environ.get("LAZYSTUDIO_ALLOW_REMOTE") == "1":
            return True
        if parsed.hostname.lower() == "localhost":
            return True
        try:
            return ip_address(parsed.hostname).is_loopback
        except ValueError:
            return False

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        caller_token: str | None = None,
    ) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if caller_token:
            headers[CALLER_TOKEN_HEADER] = caller_token
        request = Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.wait_seconds + 15) as response:
                return json.load(response)
        except HTTPError as exc:
            # Never include request headers (and therefore never the token) in errors.
            message = ""
            try:
                error_payload = json.load(exc)
                if isinstance(error_payload, dict):
                    message = str(error_payload.get("message") or error_payload.get("detail") or "")
                    run_id = error_payload.get("run_id")
                    if isinstance(run_id, str) and run_id not in message:
                        message = f"{message} (run_id={run_id})" if message else f"run_id={run_id}"
            except (OSError, ValueError, TypeError):
                pass
            suffix = f": {message}" if message else ""
            raise OSError(f"LazyStudio returned HTTP {exc.code}{suffix}") from None
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise OSError(f"LazyStudio timed out after {self.wait_seconds + 15} seconds") from None
            raise OSError(str(exc.reason)) from None
        except TimeoutError:
            raise OSError(f"LazyStudio timed out after {self.wait_seconds + 15} seconds") from None

    @staticmethod
    def _result(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise ValueError("LazyStudio returned an invalid result")
        status = payload.get("status")
        run_id = payload.get("run_id")
        if status == "running" and isinstance(run_id, str):
            return f"still running, run_id={run_id}"
        if status == "succeeded" and isinstance(payload.get("output"), str):
            return payload["output"]
        if status == "stuck":
            # LazyStudio: a nested run ignored cancellation and holds the
            # process environment; the operator must restart LazyStudio.
            detail = payload.get("message") or payload.get("error") or "nested run is stuck"
            if isinstance(detail, str):
                run = f" (run_id={run_id})" if isinstance(run_id, str) else ""
                return f"stuck: {detail}{run}"
        if status == "failed":
            detail = payload.get("output") or payload.get("error") or "unknown error"
            if isinstance(detail, str):
                run = f" (run_id={run_id})" if isinstance(run_id, str) else ""
                return f"failed: {detail}{run}"
        run = f" (run_id={run_id})" if isinstance(run_id, str) else ""
        raise ValueError(f"LazyStudio returned an invalid result{run}")

    def _call(self, name: str, task: str) -> str:
        payload = self._request(
            "POST",
            f"/api/mcp-tools/{quote(name, safe='')}/call",
            {"task": task, "wait_seconds": self.wait_seconds},
            caller_token=_caller_token(),
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

        seen = set(_RESERVED_NAMES)
        for item in self.published:
            name = item.get("name")
            if not isinstance(name, str) or not _PUBLISHED_NAME_RE.fullmatch(name) or name in seen:
                logger.warning("Skipping invalid, reserved, or duplicate Studio tool name: %r", name)
                continue
            seen.add(name)
            description = item.get("description")
            tools.append(
                self._published_tool(
                    name,
                    description if isinstance(description, str) else "Run a published LazyStudio agent.",
                )
            )
        return tools
