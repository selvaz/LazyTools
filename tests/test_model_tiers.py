"""The catalogue loader: a validated ladder of model tiers, not a config file trusted blind.

Every model named here must be one the delegation tools would actually accept -- an allow-
listed Codex model, or an Anthropic-looking Claude one -- because a name that only LOOKS
plausible sails past this loader and fails three calls later, after a human may already have
approved the job it was meant to run. The shared model policy preserves the original checks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lazytools.routing.catalogue import TIERS, ModelTiersError, load_tiers

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "model_tiers.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_real_catalogue_loads_and_covers_every_tier() -> None:
    catalogue = load_tiers(REPO_ROOT / "tests" / "fixtures" / "ceo_model_tiers.toml")

    assert set(catalogue) == set(TIERS)
    for tier in TIERS:
        assert catalogue[tier].steps, f"{tier} has no gradini"
        for step in catalogue[tier].steps:
            assert step.providers, f"{tier} has a step with no provider"


def test_the_real_catalogue_runs_the_codex_workhorse_steps_on_gpt_6_1_sol() -> None:
    """01/10: every Sol step moved from gpt-6-sol to gpt-6.1-sol with the effort untouched (xhigh -- never
    raised to max/ultra by this change); gpt-6-astra (thinking fallback) is unchanged."""
    catalogue = load_tiers(REPO_ROOT / "tests" / "fixtures" / "ceo_model_tiers.toml")

    codex_models = {
        (tier, candidate.model, candidate.effort)
        for tier in TIERS
        for step in catalogue[tier].steps
        for candidate in step.providers
        if candidate.provider == "codex"
    }
    assert ("basic", "gpt-6.1-sol", "xhigh") in codex_models
    assert ("writing", "gpt-6.1-sol", "xhigh") in codex_models
    assert ("thinking", "gpt-6.1-sol", "xhigh") in codex_models
    assert not any(model == "gpt-6-sol" for _, model, _ in codex_models)
    assert any(model == "gpt-6-astra" for _, model, _ in codex_models)
    assert not any(effort in ("max", "ultra") for _, _, effort in codex_models)


def test_gpt_6_1_sol_loads_from_a_catalogue(tmp_path: Path) -> None:
    toml = """
[basic]
[[basic.steps]]
[basic.steps.providers.codex]
model = "gpt-6.1-sol"
effort = "xhigh"

[writing]
[[writing.steps]]
[writing.steps.providers.codex]
model = "gpt-6.1-sol"
effort = "xhigh"

[thinking]
[[thinking.steps]]
[thinking.steps.providers.claude_code]
model = "claude-opus-5-5"

[critical]
[[critical.steps]]
[critical.steps.providers.claude_code]
model = "claude-opus-5-5"
"""
    catalogue = load_tiers(_write(tmp_path, toml))

    (candidate,) = catalogue["writing"].steps[0].providers
    assert (candidate.provider, candidate.model, candidate.effort) == ("codex", "gpt-6.1-sol", "xhigh")


def test_an_unknown_codex_model_is_rejected(tmp_path: Path) -> None:
    toml = """
[basic]
[[basic.steps]]
[basic.steps.providers.codex]
model = "gpt-5.1-codex-max"
[[basic.steps]]
[basic.steps.providers.claude_code]
model = "claude-haiku-4-5"

[writing]
[[writing.steps]]
[writing.steps.providers.claude_code]
model = "claude-sonnet-5"

[thinking]
[[thinking.steps]]
[thinking.steps.providers.claude_code]
model = "claude-opus-5-5"

[critical]
[[critical.steps]]
[critical.steps.providers.claude_code]
model = "claude-opus-5-5"
"""
    with pytest.raises(ModelTiersError, match=re.escape("gpt-5.1-codex-max")):
        load_tiers(_write(tmp_path, toml))


def test_an_anthropic_model_offered_to_codex_is_rejected(tmp_path: Path) -> None:
    toml = """
[basic]
[[basic.steps]]
[basic.steps.providers.codex]
model = "claude-haiku-4-5"

[writing]
[[writing.steps]]
[writing.steps.providers.claude_code]
model = "claude-sonnet-5"

[thinking]
[[thinking.steps]]
[thinking.steps.providers.claude_code]
model = "claude-opus-5-5"

[critical]
[[critical.steps]]
[critical.steps.providers.claude_code]
model = "claude-opus-5-5"
"""
    with pytest.raises(ModelTiersError, match=r"non-Anthropic|Anthropic"):
        load_tiers(_write(tmp_path, toml))


def test_a_codex_only_effort_is_rejected_for_claude_code(tmp_path: Path) -> None:
    toml = """
[basic]
[[basic.steps]]
[basic.steps.providers.claude_code]
model = "claude-sonnet-5"
effort = "ultra"

[writing]
[[writing.steps]]
[writing.steps.providers.claude_code]
model = "claude-sonnet-5"

[thinking]
[[thinking.steps]]
[thinking.steps.providers.claude_code]
model = "claude-opus-5-5"

[critical]
[[critical.steps]]
[critical.steps.providers.claude_code]
model = "claude-opus-5-5"
"""
    with pytest.raises(ModelTiersError, match="ultra"):
        load_tiers(_write(tmp_path, toml))


def test_a_tier_with_no_steps_is_rejected(tmp_path: Path) -> None:
    toml = """
[basic]
steps = []

[writing]
[[writing.steps]]
[writing.steps.providers.claude_code]
model = "claude-sonnet-5"

[thinking]
[[thinking.steps]]
[thinking.steps.providers.claude_code]
model = "claude-opus-5-5"

[critical]
[[critical.steps]]
[critical.steps.providers.claude_code]
model = "claude-opus-5-5"
"""
    with pytest.raises(ModelTiersError, match=r"no steps|no gradini"):
        load_tiers(_write(tmp_path, toml))


def test_a_missing_tier_is_rejected(tmp_path: Path) -> None:
    toml = """
[writing]
[[writing.steps]]
[writing.steps.providers.claude_code]
model = "claude-sonnet-5"

[thinking]
[[thinking.steps]]
[thinking.steps.providers.claude_code]
model = "claude-opus-5-5"

[critical]
[[critical.steps]]
[critical.steps.providers.claude_code]
model = "claude-opus-5-5"
"""
    with pytest.raises(ModelTiersError, match="basic"):
        load_tiers(_write(tmp_path, toml))
