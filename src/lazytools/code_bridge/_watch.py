"""REST-only GitHub pull-request event watcher."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lazytools.code_bridge import _gh
from lazytools.code_bridge._review import Review, ReviewVerdict, inline_finding, is_blocking_severity

WATCH_PREFIX = "code-bridge:watch:"
DEFAULT_MIN_INTERVAL = 60.0
DEFAULT_MAX_FAILURES = 3
_PR_RE = re.compile(r"^(?P<repo>[^/\s]+/[^/#\s]+)#(?P<number>[1-9]\d*)$")
_VERDICT_RE = re.compile(r"(?im)^\s*verdict:\s*(.+?)\s*$")
_HEAD_RE = re.compile(r"(?im)^\s*head:\s*([0-9a-f]+)\s*$")


# Codex review bot: hidden state JSON and the visible receipt.
_BOT_STATE_RE = re.compile(
    r'"code_review":\{"commit_sha":"(?P<sha>[0-9a-f]{7,40})"[^}]*?"has_unresolved_findings":(?P<open>true|false|null)'
    r'[^}]*?"status":"(?P<status>[a-z_]+)"'
)
_BOT_RECEIPT_RE = re.compile(r"Reviewed commit:\*\*\s*`(?P<sha>[0-9a-f]{7,40})`")
_BOT_CLEAN_RE = re.compile(r"(?i)didn.t find any major issues")


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
    if "codex" in author.casefold() or _is_review_bot(author):
        severity = inline_finding(item).severity
        if severity != "unknown":
            return f"{author} {severity}"
    return author


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _event_time(item: dict[str, Any], *names: str) -> str:
    for name in names:
        if item.get(name):
            return str(item[name])
    return _now_utc()


def _time_key(value: str) -> datetime:
    """Parse an event time for ordering; GitHub uses ``Z``, local stamps use offsets."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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
        events.append(WatchEvent(_now_utc(), pull.label, "head",
                                 f"{previous_head[:7]} -> {head[:7]}"))

    issue_comments = _paged_items(f"{base}/issues/{pull.number}/comments")
    reviews = _paged_items(f"{base}/pulls/{pull.number}/reviews")
    inline_comments = _paged_items(f"{base}/pulls/{pull.number}/comments")
    sources = (("comment", issue_comments), ("comment", reviews), ("inline", inline_comments))
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

    review = _bot_review(issue_comments, head, inline_comments)
    if review is not None:
        identity = f"review:{head}:{review.verdict}"
        if identity not in seen:
            events.append(WatchEvent(_now_utc(), pull.label, "review", f"{head[:7]} {review.verdict}"))
        fresh_seen.add(identity)
    if (pr.get("mergeable") is False or str(pr.get("mergeable_state") or "") == "dirty")             and f"conflict:{head}" not in seen:
        events.append(WatchEvent(_now_utc(), pull.label, "conflict", f"{head[:7]} conflicts with the base branch"))
        fresh_seen.add(f"conflict:{head}")
    finished = [item for item in checks if item.get("status") == "completed"]
    checks_ok = len(finished) == len(checks) and all(
        str(item.get("conclusion") or "") in {"success", "skipped", "neutral"} for item in finished)
    statuses_ok = all(str(item.get("state") or "") == "success" for item in statuses)
    has_ci = bool(checks) or bool(statuses)  # some repositories publish only commit statuses
    mergeable = pr.get("mergeable") is True  # null means GitHub has not computed it yet
    if (
        has_ci and checks_ok and statuses_ok and mergeable and f"ready:{head}" not in seen
        and review is not None and (review.verdict == "clean" or review.findings)
        and not any(is_blocking_severity(finding.severity) for finding in review.findings)
    ):
        detail = "no-blocking" if review.findings else "clean"
        events.append(WatchEvent(_now_utc(), pull.label, "ready",
                                 f"{head[:7]} checks={len(checks)} review={detail}"))
        fresh_seen.add(f"ready:{head}")

    merged = bool(pr.get("merged") or pr.get("merged_at"))
    terminal = merged or str(pr.get("state") or "").casefold() == "closed"
    terminal_kind = "merged" if merged else "closed"
    if terminal and not old.get("terminal"):
        events.append(WatchEvent(_event_time(pr, "merged_at", "closed_at", "updated_at"), pull.label,
                                 terminal_kind, str(pr.get("title") or terminal_kind)))
    unreviewed = int(old.get("unreviewed_polls", 0)) + 1 if review is None and old.get("head") == head else 0
    first_head = str(old.get("first_head") or head)
    state = {"head": head, "seen": sorted(fresh_seen), "terminal": terminal_kind if terminal else None,
             "first_head": first_head, "unreviewed_polls": unreviewed,
             "review_requested_heads": list(old.get("review_requested_heads", []))}
    return sorted(events, key=lambda event: _time_key(event.time)), terminal, state


def _bot_review(
    comments: list[dict[str, Any]], head: str, inline_comments: Iterable[dict[str, Any]] = (),
) -> Review | None:
    """Read the latest completed bot review and its current-head inline findings."""
    verdict: ReviewVerdict | None = None
    resolved = False
    head_open: str | None = None  # latest structured state for this head, across comments
    for item in comments:
        if not _is_review_bot(_author(item)):
            continue  # anyone can paste the bot's text; only the bot's own comments count
        body = str(item.get("body") or "")
        state_open = None
        for match in _BOT_STATE_RE.finditer(body):
            if head.startswith(match["sha"]) and match["status"] == "completed" and match["open"] != "null":
                state_open = match["open"]
        if state_open is not None:
            head_open = state_open
            resolved = state_open == "false"
            verdict = "clean" if resolved else "findings"
        elif (receipt := _BOT_RECEIPT_RE.search(body)) and head.startswith(receipt["sha"]):
            resolved = False
            verdict = "clean" if _BOT_CLEAN_RE.search(body) else "findings"
    if verdict is None:
        return None
    bot_inline = [item for item in inline_comments
                  if not item.get("in_reply_to_id") and _is_review_bot(_author(item))]
    if head_open is not None:
        # The bot keeps one summary comment whose state it rewrites, so the latest state for
        # this head is authoritative over receipts in either direction: a clean receipt covers
        # only the new diff (#406 showed "review clean" over an open P2), and a later receipt
        # must not resurrect findings the state marks resolved.
        resolved = head_open == "false"
        verdict = "clean" if resolved else "findings"
    # The structured state can confirm resolution of findings still visible in REST.
    # GitHub moves an inline comment's commit_id to the new head while its line still exists.
    findings = () if resolved else tuple(
        inline_finding(item) for item in bot_inline if item.get("commit_id") == head
    )
    return Review(findings=findings, verdict=verdict, reviewed_sha=head, reviewer_engine="codex")


def _is_review_bot(login: str) -> bool:
    allowed = os.environ.get("LAZYTOOLS_CODE_BRIDGE_REVIEW_BOTS", "chatgpt-codex-connector[bot],chatgpt-codex-connector")
    return login in {name.strip() for name in allowed.split(",") if name.strip()}


def _maybe_request_review(pull: PullRequest, state: dict[str, Any], text: str,
                          persist: Callable[[dict[str, Any]], None] | None = None) -> WatchEvent | None:
    """Ask the review bot once per new head that stayed unreviewed for two polls."""
    head = str(state.get("head") or "")
    requested = [str(sha) for sha in state.get("review_requested_heads", [])]
    if (not text or state.get("terminal") or head == state.get("first_head")
            or head in requested or int(state.get("unreviewed_polls", 0)) < 2):
        return None
    # Record the intent before posting: a crash after GitHub accepts the comment must
    # not lead to a second comment on restart.
    state["review_requested_heads"] = [*requested, head]
    if persist is not None:
        persist(state)
    _gh.pr_comment(pull.repo, pull.number, text)
    return WatchEvent(_now_utc(), pull.label, "review-requested", f"{head[:7]} {text}")


def poll_pull(pull: PullRequest, store: Any) -> tuple[list[WatchEvent], bool]:
    """Poll one PR, persist its observations, and return new events and terminal state."""
    events, terminal, state = _poll_pull(pull, store)
    store.write(_state_key(pull), state)
    return events, terminal


def run_watch(
    pulls: list[PullRequest], store: Any, *, interval: float, until: datetime | None,
    emit: Callable[[WatchEvent], None], sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    max_failures: int = DEFAULT_MAX_FAILURES, request_review: str | None = None,
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
            if request_review:
                key = _state_key(pull)

                def persist(current: dict[str, Any], key: str = key) -> None:
                    store.write(key, current)

                if asked := _maybe_request_review(pull, state, request_review, persist=persist):
                    events.append(asked)
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
