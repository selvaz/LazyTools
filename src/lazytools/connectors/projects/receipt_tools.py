"""Read-only publication receipt access for the projects MCP provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lazytools.code_bridge import _receipts
from lazytools.projects.task_store import TaskServiceError


class ReceiptTools:
    def __init__(self, path: Path | None, projects: tuple[str, ...] | None) -> None:
        self.path = path
        self.projects = projects

    def _read(self, receipt_id: str) -> str | None:
        if self.path is None or not self.path.is_file():
            return None
        raw = _receipts.stored_json(self.path, receipt_id)
        if raw is not None and self.projects is not None:
            task_id = json.loads(raw)["writer"]["task_id"]
            if task_id.partition("/")[0] not in self.projects:
                return None
        return raw

    def receipts_get(self, receipt_id: str) -> dict[str, Any] | None:
        """Get a signed publication receipt by ID from the projects store."""
        raw = self._read(receipt_id)
        return json.loads(raw) if raw is not None else None

    def receipts_verify(self, receipt_id: str, live: bool = False) -> dict[str, Any]:
        """Verify a stored receipt, optionally comparing its head with the live pull request."""
        try:
            raw = self._read(receipt_id)
            if raw is None:
                return {"valid": False, "reason": "not_found"}
            return _receipts.verify_json(raw, live=live)
        except (ImportError, TaskServiceError):
            return {"valid": False, "reason": "verification_unavailable"}
