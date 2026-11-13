"""Authenticated emergency-stop operations for provider cloud agents.

Credentials are loaded only for the duration of a request and are never put in
exceptions or result objects.  This module deliberately uses the providers'
HTTP endpoints rather than merely changing the bridge's local bookkeeping.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from lazytools.code_bridge._cloud import redact

CODEX_API = "https://chatgpt.com/backend-api/wham/tasks/{task_id}"
CLAUDE_API = "https://api.anthropic.com/v1/code/sessions/{session_id}/archive"
_CODEX_ID = re.compile(r"\b(task_[A-Za-z0-9_-]+)\b")
_CLAUDE_ID = re.compile(r"\b((?:session|cse)_[A-Za-z0-9_-]+)\b")


class StopError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


def credential_paths() -> dict[str, Path]:
    """Return the exact native credential files used on every supported OS."""
    home = Path.home()
    return {
        "codex": Path(os.environ.get("CODEX_HOME", home / ".codex")) / "auth.json",
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / ".credentials.json",
    }


def _nested_string(value: Any, names: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for name in names:
            found = value.get(name)
            if isinstance(found, str) and found:
                return found
        for child in value.values():
            found = _nested_string(child, names)
            if found:
                return found
    return None


def _token(provider: str) -> str:
    path = credential_paths()[provider]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        names = (("access_token", "accessToken", "id_token") if provider == "codex" else
                 ("accessToken", "access_token", "oauth_token"))
        token = _nested_string(payload, names)
    except (OSError, ValueError):
        token = None
    if not token:
        raise StopError(f"{provider}: login needed")
    return token


def _request(method: str, url: str, token: str, *, opener: Callable[..., Any] = urllib.request.urlopen) -> Any:
    request = urllib.request.Request(url, data=b"{}" if method == "POST" else None, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with opener(request, timeout=15) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise StopError("login needed") from None
        raise StopError(f"HTTP {exc.code} from cloud provider") from None
    except (OSError, ValueError) as exc:
        # Some HTTP stacks include request headers in their exception text.
        # Remove the exact in-memory credential before the general redactor.
        detail = str(exc).replace(token, "[redacted]")
        raise StopError(f"cloud request failed: {detail}") from None


def resolve_handle(value: str) -> tuple[str, str]:
    if match := _CODEX_ID.search(value):
        return "codex", match.group(1)
    if match := _CLAUDE_ID.search(value):
        return "claude", match.group(1)
    raise StopError("not a Codex task id/URL or Claude session id")


def stop(provider: str, handle: str, *, opener: Callable[..., Any] = urllib.request.urlopen,
         sleep: Callable[[float], None] = time.sleep, timeout: float = 60) -> dict[str, Any]:
    token = _token(provider)
    if provider == "claude":
        _request("POST", CLAUDE_API.format(session_id=handle), token, opener=opener)
        return {"provider": provider, "handle": handle, "status": "archive requested"}
    url = CODEX_API.format(task_id=handle)
    before = _request("GET", url, token, opener=opener)
    old = _codex_status(before)
    if old and old not in ("pending", "running", "in_progress", "queued"):
        return {"provider": provider, "handle": handle, "status": "already finished", "reason": old}
    _request("POST", url + "/cancel", token, opener=opener)
    deadline = time.monotonic() + timeout
    while True:
        state = _codex_status(_request("GET", url, token, opener=opener))
        if state not in (None, "pending", "running", "in_progress", "queued"):
            status = "stopped" if state in ("failed", "cancelled", "canceled") else "already finished"
            return {"provider": provider, "handle": handle, "status": status, "reason": state}
        if time.monotonic() >= deadline:
            return {"provider": provider, "handle": handle, "status": "failed", "reason": "stop not confirmed within 60 seconds"}
        sleep(1)


def _codex_status(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    turn = payload.get("current_assistant_turn")
    value = turn.get("turn_status") if isinstance(turn, dict) else payload.get("turn_status") or payload.get("status")
    return str(value).lower() if value else None


def codex_running() -> list[str]:
    """Ask the installed Codex CLI for account-wide tasks, including non-bridge tasks."""
    binary = os.environ.get("CODEX_BIN", "codex")
    proc = subprocess.run([binary, "cloud", "list", "--json"], capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise StopError(proc.stderr or "codex cloud list failed")
    try:
        rows = json.loads(proc.stdout)
    except ValueError as exc:
        raise StopError(f"codex cloud list returned invalid JSON: {exc}") from None
    found: list[str] = []
    for row in rows if isinstance(rows, list) else rows.get("tasks", []):
        if not isinstance(row, dict):
            continue
        state = str(row.get("status") or row.get("turn_status") or "").lower()
        handle = row.get("id") or row.get("task_id") or row.get("url")
        if state in ("pending", "running", "in_progress", "queued") and handle:
            try:
                provider, task_id = resolve_handle(str(handle))
                if provider == "codex": found.append(task_id)
            except StopError:
                pass
    return found
