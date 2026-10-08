"""Model and effort validation shared by catalogue consumers.

Defaults preserve the original account allow-list exactly, including case-sensitive
Codex identifiers, trimmed efforts, and Claude's other-provider deny-list.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from lazytools.projects.admission import Engine

_NON_ANTHROPIC_MODEL_PREFIXES = (
    "gpt-",
    "o1-",
    "o3-",
    "o4-",
    "gemini-",
    "deepseek-",
    "grok-",
    "llama-",
    "mistral-",
    "glm-",
    "kimi-",
)

_NON_ANTHROPIC_MODEL_EXACT_ALIASES = frozenset(prefix.rstrip("-") for prefix in _NON_ANTHROPIC_MODEL_PREFIXES)

_NON_OPENAI_MODEL_PREFIXES = ("claude-",)

_NON_OPENAI_MODEL_EXACT_ALIASES = frozenset({"sonnet", "opus", "haiku", "fable"})

CODEX_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna")

EFFORTS: dict[str, tuple[str, ...]] = {
    "codex": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "claude_code": ("low", "medium", "high", "xhigh", "max"),
}


@dataclass(frozen=True)
class ModelPolicy:
    """A caller may supply its own allowed Codex models and effort levels."""

    codex_models: tuple[str, ...] = CODEX_MODELS
    efforts: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: dict(EFFORTS))

    def reject_model(self, engine: Engine, model: str | None, *, fails_when: str = "when the job starts") -> str | None:
        if engine == "codex":
            return self.reject_codex_model(model, fails_when=fails_when)
        return self.reject_claude_model(model, fails_when=fails_when)

    def reject_claude_model(self, model: str | None, *, fails_when: str) -> str | None:
        if model is None:
            return None
        if not model.strip():
            return "REJECTED: model must not be blank -- omit it to use the default"
        lowered = model.strip().lower()
        if lowered in _NON_ANTHROPIC_MODEL_EXACT_ALIASES or lowered.startswith(_NON_ANTHROPIC_MODEL_PREFIXES):
            return f"REJECTED: {model!r} looks like a non-Anthropic model -- this runs on ClaudeCodeEngine, so this would report success now and only fail later, {fails_when}. Use an Anthropic model (e.g. 'sonnet', 'opus', 'haiku') or omit it to use the default."
        return None

    def reject_effort(self, effort: str | None, *, engine: str) -> str | None:
        if effort is None:
            return None
        if effort.strip() not in self.efforts[engine]:
            return f"REJECTED: effort {effort!r} is not one of {', '.join(self.efforts[engine])} for this engine -- omit it to use the default."
        return None

    def reject_codex_model(self, model: str | None, *, fails_when: str) -> str | None:
        if model is None:
            return None
        if not model.strip():
            return "REJECTED: model must not be blank -- omit it to use the default"
        lowered = model.strip().lower()
        if lowered in _NON_OPENAI_MODEL_EXACT_ALIASES or lowered.startswith(_NON_OPENAI_MODEL_PREFIXES):
            return f"REJECTED: {model!r} looks like an Anthropic model -- this runs on CodexEngine, so this would report success now and only fail later, {fails_when}. Use an OpenAI model ({', '.join(self.codex_models)}) or omit it to use the default."
        if model.strip() not in self.codex_models:
            return f"REJECTED: {model!r} is not a Codex model this account can run. Use one of, spelled exactly: {', '.join(self.codex_models)} or omit it to use the default. Nothing was delegated, so nothing was spent."
        return None


DEFAULT_POLICY = ModelPolicy()
