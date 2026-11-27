"""Structured plan fields stored on the existing blackboard tasks."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal

TaskTier = Literal["basic", "writing", "thinking", "critical"]
Placement = Literal["local", "cloud"]
TIERS = ("basic", "writing", "thinking", "critical")
PLACEMENTS = ("local", "cloud")
_TEXT_FIELDS = (
    "task_id", "parent_id", "requested_profile", "placement_reason", "preflight_ref",
    "repo", "assigned_branch", "review_of", "title",
)


def task_id(task: dict[str, Any], index: int) -> str:
    return task.get("task_id") or f"@{index}"


def routing_fields(data: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    preference = data.get("engine_preference")
    if preference is not None:
        if not isinstance(preference, (list, tuple)) or any(
            engine not in ("codex", "claude", "claude_code") for engine in preference
        ):
            raise ValueError("engine_preference must be a list of codex or claude engines")
        normalised = ["claude" if engine == "claude_code" else engine for engine in preference]
        if len(set(normalised)) != len(normalised):
            raise ValueError("engine_preference must not repeat an engine")
        fields["engine_preference"] = normalised
    fallback = data.get("fallback")
    if fallback is not None:
        if fallback not in ("wait", "allow"):
            raise ValueError("fallback must be wait or allow")
        fields["fallback"] = fallback
    return fields


def plan_fields(task: dict[str, Any]) -> dict[str, Any]:
    fields = routing_fields(task)
    for name in _TEXT_FIELDS:
        value = task.get(name)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
            fields[name] = value.strip()
    if "task_id" in fields and not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", fields["task_id"]):
        raise ValueError("task_id must contain only letters, digits, underscores, periods or hyphens")
    for name, choices in (
        ("tier", TIERS), ("requested_placement", PLACEMENTS), ("effective_placement", PLACEMENTS)
    ):
        value = task.get(name)
        if value is not None:
            if value not in choices:
                raise ValueError(f"invalid {name}: {value!r}")
            fields[name] = value
    network = task.get("requested_network")
    if network is not None:
        if not isinstance(network, bool):
            raise ValueError("requested_network must be a boolean")
        fields["requested_network"] = network
    if "max_check_retries" in task:
        retries = task["max_check_retries"]
        if type(retries) is not int or retries < 0:
            raise ValueError("max_check_retries must be a nonnegative integer")
        fields["max_check_retries"] = retries
    for name in ("dependency_ids", "acceptance_criteria", "checks", "touches"):
        if name in task:
            values = task[name]
            if not isinstance(values, (tuple, list)) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"{name} must be a list of nonempty strings")
            fields[name] = [value.strip() for value in values]
            if name == "touches" and any(
                value.startswith(("/", "\\")) or "\\" in value
                or any(part in ("", ".", "..") for part in value.split("/"))
                or re.match(r"^[a-zA-Z]:", value) for value in fields[name]
            ):
                raise ValueError("touches must contain repo-relative paths or globs using / separators")
    return fields


def _glob_tokens(pattern: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            tokens.append("**/")
            i += 3
        elif pattern.startswith("**", i):
            tokens.append("**")
            i += 2
        elif pattern[i] == "[":
            start = i + 1
            if start < len(pattern) and pattern[start] == "!":
                start += 1
            if start < len(pattern) and pattern[start] == "]":
                start += 1
            end = pattern.find("]", start)
            if end == -1:
                tokens.append("[")
                i += 1
            else:
                tokens.append(pattern[i:end + 1])
                i = end + 1
        else:
            tokens.append(pattern[i])
            i += 1
    return tokens


def _glob_intersects(left: str, right: str) -> bool:
    """Check whether two path globs match at least one common path.

    Intersect their finite-state matchers; wildcards never cross / except **.
    """
    patterns = (_glob_tokens(left), _glob_tokens(right))
    # Character classes change membership only at their literal/range boundaries.
    points = set(range(128)) | {0x10ffff}
    for char in left + right:
        points.update(n for n in (ord(char) - 1, ord(char), ord(char) + 1) if 0 <= n <= 0x10ffff)
    alphabet = [chr(n) for n in points if n != 0 and not 0xd800 <= n <= 0xdfff]

    def edges(tokens: list[str], state: tuple[int, bool]) -> tuple[list[tuple[int, bool]], list[tuple[str, tuple[int, bool]]]]:
        index, inside = state
        if index == len(tokens):
            return [], []
        token = tokens[index]
        if token == "**/":
            if inside:
                return [], [("**", state), ("/", (index, False))]
            return [(index + 1, False)], [("**", (index, True)), ("/", state)]
        if token in ("*", "**"):
            return [(index + 1, False)], [(token, state)]
        return [], [(token, (index + 1, False))]

    def matches(token: str, char: str) -> bool:
        if token == "**":
            return True
        if token == "*" or token == "?" or token.startswith("["):
            return char != "/" and fnmatchcase(char, token)
        return token == char

    pending = [((0, False), (0, False))]
    seen = set(pending)
    while pending:
        a, b = pending.pop()
        if a[0] == len(patterns[0]) and b[0] == len(patterns[1]):
            return True
        ae, ac = edges(patterns[0], a)
        be, bc = edges(patterns[1], b)
        successors = [(n, b) for n in ae] + [(a, n) for n in be]
        successors += [(an, bn) for at, an in ac for bt, bn in bc
                       if any(matches(at, c) and matches(bt, c) for c in alphabet)]
        for pair in successors:
            if pair not in seen:
                seen.add(pair)
                pending.append(pair)
    return False


def touches_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # GitHub repository names are case-insensitive: Owner/Repo and owner/repo are the same checkout.
    # A missing repo means the default checkout, so two tasks without one share a repository too.
    repo = str(left.get("repo") or "").casefold()
    return bool(repo == str(right.get("repo") or "").casefold() and any(
        _glob_intersects(a, b) for a in left.get("touches", []) for b in right.get("touches", [])
    ))


def touches_warnings(tasks: list[dict[str, Any]]) -> list[str]:
    graph = {task_id(task, i): task.get("dependency_ids", []) for i, task in enumerate(tasks)}

    def depends_on(start: str, target: str) -> bool:
        pending, seen = list(graph[start]), set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(graph.get(current, []))
        return False

    warnings = []
    for i, left in enumerate(tasks):
        for j in range(i + 1, len(tasks)):
            a, b = task_id(left, i), task_id(tasks[j], j)
            if not depends_on(a, b) and not depends_on(b, a) and touches_overlap(left, tasks[j]):
                warnings.append(f"touches overlap between {a} and {b} without a dependency path")
    return warnings


def validate_dependencies(tasks: list[dict[str, Any]]) -> None:
    graph = {task_id(task, index): task.get("dependency_ids", []) for index, task in enumerate(tasks)}
    if len(graph) != len(tasks):
        raise ValueError("duplicate task_id")
    for index, task in enumerate(tasks):
        if task.get("review_of") and task["review_of"] not in graph[task_id(task, index)]:
            raise ValueError("review_of must also be listed in dependency_ids")
    for identifier, dependencies in graph.items():
        for dependency in dependencies:
            if dependency not in graph:
                raise ValueError(f"unknown dependency {dependency!r} for task {identifier!r}")
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise ValueError(f"dependency cycle: {' -> '.join(exc.args[1])}") from exc
