"""Structured plan fields stored on the existing blackboard tasks."""

from __future__ import annotations

import re
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal

TaskTier = Literal["basic", "writing", "thinking", "critical"]
Placement = Literal["local", "cloud"]
TIERS = ("basic", "writing", "thinking", "critical")
PLACEMENTS = ("local", "cloud")
_TEXT_FIELDS = (
    "task_id", "parent_id", "requested_profile", "placement_reason", "preflight_ref",
    "repo", "assigned_branch", "review_of",
)


def task_id(task: dict[str, Any], index: int) -> str:
    return task.get("task_id") or f"@{index}"


def plan_fields(task: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
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
    for name in ("dependency_ids", "acceptance_criteria"):
        if name in task:
            values = task[name]
            if not isinstance(values, (tuple, list)) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"{name} must be a list of nonempty strings")
            fields[name] = [value.strip() for value in values]
    return fields


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
