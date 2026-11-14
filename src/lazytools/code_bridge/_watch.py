"""REST-only GitHub pull-request event watcher."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lazytools.code_bridge import _gh

WATCH_PREFIX = "code-bridge:watch:"
DEFAULT_MIN_INTERVAL = 60.0
DEFAULT_MAX_FAILURES = 3
_PR_RE = re.compile(r"^(?P<repo>[^/\s]+/[^/#\s]+)#(?P<number>[1-9]\d*)$")
_VERDICT_RE = re.compile(r"(?im)^\s*verdict:\s*(.+?)\s*$")
_HEAD_RE = re.compile(r"(?im)^\s*head:\s*([0-9a-f]+)\s*$")
_BADGE_RE = re.compile(r"(?i)(?:\b|\[)(P[0-3])(?:\b|\])")


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int

    @property
    def label(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class WatchEvent:
    time: str
    pull: str
    kind: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"time": self.time, "pull": self.pull, "kind": self.kind, "detail": self.detail}


def parse_pull(value: str) -> PullRequest:
    match = _PR_RE.fullmatch(value)
    if not match:
        raise ValueError(f"invalid pull request {value!r}; expected owner/repo#number")
    return PullRequest(match["repo"], int(match["number"]))


def minimum_interval() -> float:
    raw = os.environ.get("LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL", str(DEFAULT_MIN_INTERVAL))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL must be a number") from exc
    if value <= 0:
        raise ValueError("LAZYTOOLS_CODE_BRIDGE_WATCH_MIN_INTERVAL must be positive")
    return value


def _api(path: str) -> Any:
    raw = _gh.run_gh("api", path)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"gh api {path} returned invalid JSON") from exc


def _items(payload: Any, key: str | None = None) -> list[dict[str, Any]]:
    if key and isinstance(payload, dict):
        payload = payload.get(key)
    return [item for item in payload or [] if isinstance(item, dict)] if isinstance(payload, list) else []


def _paged_items(path: str, key: str | None = None) -> list[dict[str, Any]]:
    """Read every REST page; ``--slurp`` keeps the resulting JSON parseable."""
    raw = _gh.run_gh("api", path, "--paginate", "--slurp")
    try:
        pages = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"gh api {path} returned invalid JSON") from exc
    if not isinstance(pages, list):
        return []
    result: list[dict[str, Any]] = []
    for page in pages:
        result.extend(_items(page, key))
    return result


def _author(item: dict[str, Any]) -> str:
    user = item.get("user")
    return str(user.get("login") or "unknown") if isinstance(user, dict) else "unknown"


def _comment_detail(item: dict[str, Any]) -> str:
    author = _author(item)
    body = str(item.get("body") or "")
    badge = _BADGE_RE.search(body) if "codex" in author.casefold() else None
    return f"{author}{f' {badge.group(1).upper()}' if badge else ''}"


def _event_time(item: dict[str, Any], *names: str) -> str:
    for name in names:
        if item.get(name):
            return str(item[name])
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _comment_event(pull: PullRequest, kind: str, item: dict[str, Any], *, current_head: str) -> WatchEvent:
    body = str(item.get("body") or "")
    if "<!-- independent-review v1 -->" in body:
        verdict = _VERDICT_RE.search(body)
        head = _HEAD_RE.search(body)
        marker_head = head.group(1) if head else "unknown"
        detail = f"verdict={verdict.group(1).strip() if verdict else 'unknown'} head={marker_head}"
        if marker_head == "unknown" or not current_head.casefold().startswith(marker_head.casefold()):
            detail += " stale=true"
        kind = "verdict"
    else:
        detail = _comment_detail(item)
    return WatchEvent(_event_time(item, "submitted_at", "created_at", "updated_at"), pull.label, kind, detail)


def _state_key(pull: PullRequest) -> str:
    return f"{WATCH_PREFIX}{pull.label}"


def _poll_pull(pull: PullRequest, store: Any) -> tuple[list[WatchEvent], bool, dict[str, Any]]:
    old = store.read(_state_key(pull), {})
    old = old if isinstance(old, dict) else {}
    seen = set(str(value) for value in old.get("seen", []))
    fresh_seen = set(seen)
    events: list[WatchEvent] = []
    base = f"repos/{pull.repo}"
    pr = _api(f"{base}/pulls/{pull.number}")
    if not isinstance(pr, dict) or not isinstance(pr.get("head"), dict):
        raise ValueError(f"GitHub returned an unexpected payload for {pull.label}")
    head = str(pr["head"].get("sha") or "")
    previous_head = str(old.get("head") or "")
    if previous_head and previous_head != head:
        events.append(WatchEvent(datetime.now().astimezone().isoformat(timespec="seconds"), pull.label, "head",
                                 f"{previous_head[:7]} -> {head[:7]}"))

    sources: Iterable[tuple[str, list[dict[str, Any]]]] = (
        ("comment", _paged_items(f"{base}/issues/{pull.number}/comments")),
        ("comment", _paged_items(f"{base}/pulls/{pull.number}/reviews")),
        ("inline", _paged_items(f"{base}/pulls/{pull.number}/comments")),
    )
    for kind, items in sources:
        for item in items:
            identity = f"{kind}:{item.get('id')}"
            commit = item.get("commit_id")
            if identity not in seen and (not commit or str(commit) == head):
                events.append(_comment_event(pull, kind, item, current_head=head))
            fresh_seen.add(identity)

    checks = _paged_items(f"{base}/commits/{head}/check-runs", "check_runs")
    for item in checks:
        identity = f"check:{item.get('id')}"
        conclusion = str(item.get("conclusion") or "")
        if identity not in seen and item.get("status") == "completed" and conclusion in {"success", "failure"}:
            events.append(WatchEvent(_event_time(item, "completed_at"), pull.label, "check",
                                     f"{item.get('name') or 'check'} {conclusion}"))
        if item.get("status") == "completed" and conclusion in {"success", "failure"}:
            fresh_seen.add(identity)
    statuses = _paged_items(f"{base}/commits/{head}/status", "statuses")
    for item in statuses:
        identity = f"status:{item.get('id', str(item.get('context')) + ':' + str(item.get('updated_at')))}"
        result = str(item.get("state") or "")
        if identity not in seen and result in {"success", "failure"}:
            events.append(WatchEvent(_event_time(item, "updated_at", "created_at"), pull.label, "check",
                                     f"{item.get('context') or 'status'} {result}"))
        if result in {"success", "failure"}:
            fresh_seen.add(identity)

    merged = bool(pr.get("merged") or pr.get("merged_at"))
    terminal = merged or str(pr.get("state") or "").casefold() == "closed"
    terminal_kind = "merged" if merged else "closed"
    if terminal and not old.get("terminal"):
        events.append(WatchEvent(_event_time(pr, "merged_at", "closed_at", "updated_at"), pull.label,
                                 terminal_kind, str(pr.get("title") or terminal_kind)))
    state = {"head": head, "seen": sorted(fresh_seen), "terminal": terminal_kind if terminal else None}
    return sorted(events, key=lambda event: event.time), terminal, state


def poll_pull(pull: PullRequest, store: Any) -> tuple[list[WatchEvent], bool]:
    """Poll one PR, persist its observations, and return new events and terminal state."""
    events, terminal, state = _poll_pull(pull, store)
    store.write(_state_key(pull), state)
    return events, terminal


def run_watch(
    pulls: list[PullRequest], store: Any, *, interval: float, until: datetime | None,
    emit: Callable[[WatchEvent], None], sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    max_failures: int = DEFAULT_MAX_FAILURES,
) -> int:
    delay = max(interval, minimum_interval())
    terminal = {
        pull.label
        for pull in pulls
        if isinstance((state := store.read(_state_key(pull), {})), dict) and state.get("terminal")
    }
    failures = {pull.label: 0 for pull in pulls}
    while True:
        if until is not None and now() >= until:
            return 0
        if len(terminal) == len(pulls):
            return 2
        failed_attempts = 0
        for pull in pulls:
            if pull.label in terminal:
                continue
            try:
                events, done, state = _poll_pull(pull, store)
            except (OSError, ValueError, RuntimeError):
                failures[pull.label] += 1
                failed_attempts = failures[pull.label]
                if failed_attempts >= max_failures:
                    raise
                break
            failures[pull.label] = 0
            for event in events:
                emit(event)
            store.write(_state_key(pull), state)
            if done:
                terminal.add(pull.label)
        if len(terminal) == len(pulls):
            return 2
        backoff = delay * (2 ** (failed_attempts - 1)) if failed_attempts else delay
        remaining = (until - now()).total_seconds() if until is not None else backoff
        if remaining <= 0:
            return 0
        sleep(min(backoff, remaining))


__all__ = ["PullRequest", "WatchEvent", "minimum_interval", "parse_pull", "poll_pull", "run_watch"]
