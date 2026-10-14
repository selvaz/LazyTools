"""Frozen source oracle: compare complete decisions with the unmodified CEO logic.

Only import destinations change. Tests require no sibling checkout or live quota.
"""

from __future__ import annotations

import ast
import json
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lazytools.projects.admission import EngineBudget, TelemetryReading, WindowReading
from lazytools.routing import DEFAULT_POLICY, CapabilityRequirement, ContinuityHint, load_tiers, route

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
FIXTURE = json.loads((Path(__file__).parent / "fixtures/routing_parity.json").read_text(encoding="utf-8"))


def _reference():
    tree = ast.parse(FIXTURE["router"])
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            node.module = {
                "lazyceo.admission": "lazytools.projects.admission",
                "lazyceo.model_tiers": "lazytools.routing.catalogue",
            }.get(node.module, node.module)
    module = types.ModuleType("_frozen_ceo_router")
    sys.modules[module.__name__] = module
    exec(compile(tree, f"LazyCEO@{FIXTURE['source_commit']}:model_router.py", "exec"), module.__dict__)
    policy = {}
    exec(
        compile(
            "from __future__ import annotations\n" + FIXTURE["policy"],
            f"LazyCEO@{FIXTURE['source_commit']}:validators",
            "exec",
        ),
        policy,
    )
    return module, policy


ORIGINAL, POLICY = _reference()


@pytest.mark.parametrize("tier", ["basic", "writing", "thinking", "critical", "unknown"])
@pytest.mark.parametrize(
    "scenario",
    [
        "weekly",
        "tie",
        "hysteresis",
        "missing_codex",
        "missing_both",
        "error",
        "stale",
        "future",
        "exhausted",
        "continuity",
        "failed_twice",
        "images",
        "audio",
        "codex_only",
        "claude_only",
        "unavailable",
        "review_codex",
        "review_claude",
        "review_missing",
        "in_flight",
        "no_budget",
        "forecast",
    ],
)
def test_complete_decision_matches_original(tier, scenario):
    catalogue = load_tiers(Path(__file__).parent / "fixtures/ceo_model_tiers.toml")
    readings = {
        "codex": TelemetryReading(
            "codex", "fixture", NOW, (WindowReading("codex/10080m", 10, 10080), WindowReading("codex/300m", 50, 300))
        ),
        "claude_code": TelemetryReading(
            "claude_code",
            "fixture",
            NOW,
            (WindowReading("weekly/all models", 40, 10080), WindowReading("session", 1, 300)),
        ),
    }
    budgets = {engine: EngineBudget(engine) for engine in readings}
    kwargs = {"catalogue": catalogue, "readings": readings, "budgets": budgets, "in_flight": {}, "now": NOW}
    if scenario in ("tie", "hysteresis"):
        readings["claude_code"] = replace(
            readings["claude_code"],
            windows=(WindowReading("weekly/all models", 10 if scenario == "tie" else 15, 10080),),
        )
    elif scenario == "missing_codex":
        readings.pop("codex")
    elif scenario == "missing_both":
        readings.clear()
    elif scenario == "error":
        readings["codex"] = replace(readings["codex"], error="offline")
    elif scenario in ("stale", "future"):
        readings["codex"] = replace(
            readings["codex"], observed_at=NOW + timedelta(seconds=-22000 if scenario == "stale" else 121)
        )
    elif scenario in ("exhausted", "forecast"):
        for engine, reading in readings.items():
            windows = (
                WindowReading(
                    reading.windows[0].window_id,
                    100 if scenario == "exhausted" else 40,
                    10080,
                    None if scenario == "exhausted" else NOW + timedelta(days=5),
                ),
            )
            readings[engine] = replace(reading, windows=windows)
    elif scenario in ("continuity", "failed_twice"):
        kwargs["continuity"] = ContinuityHint("claude_code", 2 if scenario == "failed_twice" else 0)
    elif scenario in ("images", "audio"):
        kwargs["capability"] = CapabilityRequirement(images=scenario == "images", audio=scenario == "audio")
    elif scenario in ("codex_only", "claude_only", "unavailable"):
        kwargs["available"] = (
            frozenset()
            if scenario == "unavailable"
            else frozenset(("codex" if scenario == "codex_only" else "claude_code",))
        )
    elif scenario.startswith("review"):
        kwargs["writer_provider_for_review"] = "claude_code" if scenario == "review_claude" else "codex"
        if scenario == "review_missing":
            readings.pop("claude_code")
    elif scenario == "in_flight":
        kwargs["in_flight"] = {"codex": 65, "claude_code": 2}
    elif scenario == "no_budget":
        budgets.clear()
    assert route(tier, **kwargs).as_record() == ORIGINAL.route(tier, **kwargs).as_record()


@pytest.mark.parametrize("engine", ["codex", "claude_code"])
@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        " ",
        "gpt-6.1-sol",
        " gpt-6.1-sol ",
        "GPT-6.1-SOL",
        "gpt-5.1-codex-max",
        "sonnet",
        "OPUS",
        "claude-opus-5-5",
        "fable",
        "o1",
        "o3",
        "deepseek",
        "future-alias",
    ],
)
def test_policy_acceptance_matches_original_validator(engine, model):
    reject = POLICY["_reject_non_openai_model" if engine == "codex" else "_reject_non_anthropic_model"]
    before = reject(model, fails_when="when the job starts")
    after = DEFAULT_POLICY.reject_model(engine, model)
    assert (before is None) == (after is None)


@pytest.mark.parametrize("engine", ["codex", "claude_code"])
@pytest.mark.parametrize(
    "effort", [None, "", "low", " medium ", "high", "xhigh", "max", "ultra", "none", "minimal", "HIGH"]
)
def test_effort_rejection_is_identical_to_original(engine, effort):
    assert DEFAULT_POLICY.reject_effort(effort, engine=engine) == POLICY["_reject_bad_effort"](effort, engine=engine)
