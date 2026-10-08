from __future__ import annotations

from dataclasses import replace
from importlib.resources import as_file, files
from pathlib import Path

import pytest

from lazytools.routing import DEFAULT_POLICY, ModelTiersError, load_default_tiers, load_tiers
from lazytools.routing.policy import CODEX_MODELS, EFFORTS


@pytest.mark.parametrize("model", CODEX_MODELS)
def test_original_codex_allowlist(model):
    assert DEFAULT_POLICY.reject_model("codex", f" {model} ") is None
    assert DEFAULT_POLICY.reject_model("codex", model.upper()) is not None


@pytest.mark.parametrize("engine", ["codex", "claude_code"])
def test_original_efforts_and_whitespace(engine):
    for effort in EFFORTS[engine]:
        assert DEFAULT_POLICY.reject_effort(f" {effort} ", engine=engine) is None
        assert DEFAULT_POLICY.reject_effort(effort.upper(), engine=engine) is not None
    for invalid in ("", "none", "minimal", "huge"):
        assert DEFAULT_POLICY.reject_effort(invalid, engine=engine) is not None
    assert DEFAULT_POLICY.reject_effort(None, engine=engine) is None


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6-astra",
        " GPT-6.1-SOL ",
        "o1",
        "o3",
        "o4",
        "gemini",
        "deepseek-chat",
        "grok",
        "llama-9",
        "mistral",
        "glm",
        "kimi",
    ],
)
def test_claude_rejects_other_providers(model):
    assert DEFAULT_POLICY.reject_model("claude_code", model) is not None


@pytest.mark.parametrize("model", [None, "sonnet", "opus", "haiku", "fable", "claude-opus-5-5", "future-claude-alias"])
def test_claude_keeps_original_permissive_alias_check(model):
    assert DEFAULT_POLICY.reject_model("claude_code", model) is None


@pytest.mark.parametrize("model", ["sonnet", "OPUS", "haiku", "fable", "claude-opus-5-5", "gpt-5.1-codex-max", ""])
def test_codex_rejects_wrong_provider_unknown_and_blank(model):
    assert DEFAULT_POLICY.reject_model("codex", model) is not None


def test_unset_and_blank_models():
    assert DEFAULT_POLICY.reject_model("codex", None) is None
    assert DEFAULT_POLICY.reject_model("claude_code", " ") is not None


def _packaged():
    with as_file(files("lazytools.routing").joinpath("default_tiers.toml")) as path:
        return load_tiers(path)


def test_packaged_catalogue_matches_contract_table():
    catalogue = _packaged()
    expected = {
        "basic": [[("codex", "gpt-6.1-sol", "medium"), ("claude_code", "claude-sonnet-5-5", "medium")]],
        "writing": [
            [("codex", "gpt-6.1-sol", "high"), ("claude_code", "claude-sonnet-5-5", "high")],
            [("codex", "gpt-6.1-sol", "xhigh"), ("claude_code", "claude-opus-5-5", "high")],
        ],
        "thinking": [
            [("codex", "gpt-6.1-sol", "xhigh"), ("claude_code", "claude-opus-5-5", "medium")],
            [("claude_code", "claude-opus-5-5", "high")],
            [("codex", "gpt-6-astra", "high")],
        ],
        "critical": [
            [("claude_code", "claude-opus-5-5", "high"), ("codex", "gpt-6-astra", "high")],
            [("claude_code", "claude-opus-5-5", "max"), ("codex", "gpt-6-astra", "xhigh")],
        ],
    }
    assert {
        tier: [[(p.provider, p.model, p.effort) for p in step.providers] for step in entry.steps]
        for tier, entry in catalogue.items()
    } == expected


def test_default_catalogue_uses_home_override_and_reports_broken_override(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert load_default_tiers() == _packaged()
    folder = tmp_path / ".lazytools"
    folder.mkdir()
    override = folder / "model_tiers.toml"
    override.write_text(
        (files("lazytools.routing").joinpath("default_tiers.toml").read_text()).replace(
            'effort = "medium"', 'effort = "low"'
        )
    )
    assert load_default_tiers()["basic"].steps[0].providers[0].effort == "low"
    override.write_text("broken")
    with pytest.raises(ModelTiersError, match="valid TOML"):
        load_default_tiers()


def test_custom_policy_is_used_by_loader(tmp_path):
    path = tmp_path / "tiers.toml"
    text = files("lazytools.routing").joinpath("default_tiers.toml").read_text()
    path.write_text(
        text.replace("gpt-6.1-sol", "custom-codex").replace('effort = "medium"', 'effort = "custom-effort"')
    )
    policy = replace(
        DEFAULT_POLICY,
        codex_models=(*CODEX_MODELS, "custom-codex"),
        efforts={engine: (*efforts, "custom-effort") for engine, efforts in EFFORTS.items()},
    )
    assert load_tiers(path, policy=policy)["basic"].steps[0].providers[0].model == "custom-codex"
    with pytest.raises(ModelTiersError):
        load_tiers(path)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda text: text + "\n[unknown]\n", "unknown tier"),
        (lambda text: text.replace('effort = "medium"', "effort = 12"), "effort must be a string"),
        (lambda text: text.replace('model = "gpt-6.1-sol"', 'model = " "'), "non-empty string"),
        (lambda text: text.replace("providers.codex", "providers.typo"), "unknown provider"),
        (lambda text: text.replace("[basic]", "[basic]\nnote = 12"), "note must be a string"),
        (
            lambda text: text.replace("[[basic.steps]]", "[[basic.steps]]\ndescription = 12"),
            "description must be a string",
        ),
        (lambda text: text.replace('model = "gpt-6.1-sol"', 'other = "gpt-6.1-sol"'), "needs a 'model'"),
        (lambda text: text.replace("[[basic.steps]]", "steps = [1]"), "must be a table|valid TOML"),
    ],
)
def test_catalogue_loading_errors(tmp_path, mutate, match):
    path = tmp_path / "tiers.toml"
    path.write_text(mutate(files("lazytools.routing").joinpath("default_tiers.toml").read_text()))
    with pytest.raises(ModelTiersError, match=match):
        load_tiers(path)


def test_missing_file_and_invalid_utf8_are_catalogue_errors(tmp_path):
    path = tmp_path / "missing.toml"
    with pytest.raises(ModelTiersError, match="could not read"):
        load_tiers(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ModelTiersError, match="valid TOML"):
        load_tiers(path)


def test_empty_provider_table_is_rejected(tmp_path):
    path = tmp_path / "tiers.toml"
    path.write_text("[basic]\n[[basic.steps]]\n[writing]\n[thinking]\n[critical]\n")
    with pytest.raises(ModelTiersError, match="no providers"):
        load_tiers(path)


@pytest.mark.parametrize("model", CODEX_MODELS)
def test_default_model_efforts_match_live_codex_capabilities(model):
    allowed = DEFAULT_POLICY.efforts_for("codex", model)
    assert allowed == EFFORTS["codex"][:5] if model.endswith("-luna") else allowed == EFFORTS["codex"]
    assert DEFAULT_POLICY.reject_effort(" max ", engine="codex", model=f" {model} ") is None
    rejection = DEFAULT_POLICY.reject_effort("ultra", engine="codex", model=model)
    assert (rejection is not None) == model.endswith("-luna")
    if rejection:
        assert model in rejection


def test_custom_model_efforts_and_engine_fallback():
    policy = replace(DEFAULT_POLICY, codex_model_efforts={"custom": ("high",)})
    assert policy.reject_effort("medium", engine="codex", model="custom") is not None
    assert policy.reject_effort(" high ", engine="codex", model=" custom ") is None
    assert policy.reject_effort("ultra", engine="codex", model="unknown") is None
    assert policy.reject_effort("ultra", engine="codex") is None
    assert policy.reject_effort("ultra", engine="claude_code", model="custom") is not None
    assert replace(policy, codex_model_efforts={}).reject_effort("medium", engine="codex", model="custom") is None


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5.6-luna"])
def test_catalogue_uses_model_specific_efforts(tmp_path, model):
    path = tmp_path / "tiers.toml"
    text = files("lazytools.routing").joinpath("default_tiers.toml").read_text()
    text = text.replace("gpt-6.1-sol", model).replace('effort = "medium"', 'effort = "ultra"')
    path.write_text(text)
    with pytest.raises(ModelTiersError, match=model):
        load_tiers(path)
    path.write_text(text.replace('effort = "ultra"', 'effort = "max"'))
    assert load_tiers(path)["basic"].steps[0].providers[0].effort == "max"
