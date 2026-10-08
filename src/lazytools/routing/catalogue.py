"""Validated capability ladders: reject unusable models before a job starts."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from lazytools.projects.admission import Engine
from lazytools.routing.policy import DEFAULT_POLICY, ModelPolicy

#: The only four tiers this file may name. An unknown tier is rejected rather than silently
#: carried along -- the router indexes the catalogue by tier name, and a typo here would
#: otherwise surface as "tier has no steps" three modules away.
TIERS: tuple[str, ...] = ("basic", "writing", "thinking", "critical")

#: Packaged catalogue; load_default_tiers() checks the operator's override first.
DEFAULT_PATH = Path(__file__).with_name("default_tiers.toml")

_PROVIDERS: tuple[Engine, ...] = ("codex", "claude_code")


class ModelTiersError(ValueError):
    """The catalogue file is malformed, or names a model/effort this account cannot run."""


@dataclass(frozen=True)
class StepModel:
    """One provider's offering at one step: what to run, and how hard to think."""

    provider: Engine
    model: str
    effort: str | None = None


@dataclass(frozen=True)
class TierStep:
    """One rung of a tier's ladder. ``providers`` has one entry per provider offered at
    this step -- one when only one provider has a model here, two when the router is meant
    to choose between them by quota (design section B)."""

    providers: tuple[StepModel, ...]
    description: str | None = None

    def for_provider(self, provider: Engine) -> StepModel | None:
        for candidate in self.providers:
            if candidate.provider == provider:
                return candidate
        return None


@dataclass(frozen=True)
class TierCatalogue:
    """One tier's full ladder, cheapest-capable step first."""

    name: str
    steps: tuple[TierStep, ...]
    note: str | None = None


def _validate_model(provider: Engine, model: str, *, tier: str, step_index: int, policy: ModelPolicy) -> None:
    """Raise ``ModelTiersError`` unless ``model`` is one this account can actually run on
    ``provider`` -- reusing the exact checks the delegation tools apply per call, so the
    catalogue and the runtime enforcement can never quietly drift apart."""

    where = f"{tier}.steps[{step_index}].providers.{provider}"
    if provider == "codex":
        rejection = policy.reject_codex_model(model, fails_when=f"once {where} is actually delegated")
    else:
        rejection = policy.reject_claude_model(model, fails_when=f"once {where} is actually delegated")
    if rejection is not None:
        raise ModelTiersError(f"{where}: {rejection}")


def _validate_effort(provider: Engine, effort: str | None, *, tier: str, step_index: int, policy: ModelPolicy) -> None:

    if effort is None:
        return
    rejection = policy.reject_effort(effort, engine=provider)
    if rejection is not None:
        raise ModelTiersError(f"{tier}.steps[{step_index}].providers.{provider}: {rejection}")


def _parse_step(raw: Any, *, tier: str, step_index: int, policy: ModelPolicy) -> TierStep:
    if not isinstance(raw, dict):
        raise ModelTiersError(f"{tier}.steps[{step_index}] must be a table")
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise ModelTiersError(f"{tier}.steps[{step_index}].description must be a string")
    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, dict) or not providers_raw:
        raise ModelTiersError(
            f"{tier}.steps[{step_index}] has no providers -- every step needs at least one "
            "provider's model (a tier with no steps, or a step with no providers, is a step "
            "the router could never route anyone to)"
        )
    models: list[StepModel] = []
    for provider_name, entry in providers_raw.items():
        if provider_name not in _PROVIDERS:
            raise ModelTiersError(
                f"{tier}.steps[{step_index}].providers.{provider_name}: unknown provider "
                f"(must be one of {', '.join(_PROVIDERS)})"
            )
        provider: Engine = provider_name  # type: ignore[assignment]
        if not isinstance(entry, dict) or "model" not in entry:
            raise ModelTiersError(f"{tier}.steps[{step_index}].providers.{provider_name} needs a 'model'")
        model = entry["model"]
        if not isinstance(model, str) or not model.strip():
            raise ModelTiersError(
                f"{tier}.steps[{step_index}].providers.{provider_name}.model must be a non-empty string"
            )
        effort = entry.get("effort")
        if effort is not None and not isinstance(effort, str):
            raise ModelTiersError(f"{tier}.steps[{step_index}].providers.{provider_name}.effort must be a string")
        _validate_model(provider, model.strip(), tier=tier, step_index=step_index, policy=policy)
        _validate_effort(provider, effort, tier=tier, step_index=step_index, policy=policy)
        models.append(StepModel(provider=provider, model=model.strip(), effort=effort))
    return TierStep(providers=tuple(models), description=description)


def _parse_tier(name: str, raw: Any, *, policy: ModelPolicy) -> TierCatalogue:
    if not isinstance(raw, dict):
        raise ModelTiersError(f"[{name}] must be a table")
    note = raw.get("note")
    if note is not None and not isinstance(note, str):
        raise ModelTiersError(f"{name}.note must be a string")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ModelTiersError(
            f"[{name}] has no steps -- a tier with no gradini is a tier the router can never route anyone to"
        )
    steps = tuple(_parse_step(step, tier=name, step_index=i, policy=policy) for i, step in enumerate(steps_raw))
    return TierCatalogue(name=name, steps=steps, note=note)


def load_tiers(path: Path, *, policy: ModelPolicy = DEFAULT_POLICY) -> dict[str, TierCatalogue]:
    """Load and validate a capability catalogue at ``path``.

    Every tier in ``TIERS`` must be present with at least one step, every step at least one
    provider, and every (provider, model, effort) must be one the corresponding delegation
    path would actually accept -- an unknown model or tier-less fascia fails HERE, at load
    time, rather than surfacing three calls later as a routing decision nobody can act on.
    """
    resolved = Path(path)
    try:
        raw_bytes = resolved.read_bytes()
    except OSError as exc:
        raise ModelTiersError(f"could not read {resolved}: {exc}") from exc
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ModelTiersError(f"{resolved} is not valid TOML: {exc}") from exc

    missing = [tier for tier in TIERS if tier not in data]
    if missing:
        raise ModelTiersError(f"{resolved} is missing tier(s): {', '.join(missing)}")
    extra = [key for key in data if key not in TIERS]
    if extra:
        raise ModelTiersError(
            f"{resolved} names unknown tier(s): {', '.join(extra)} (must be one of {', '.join(TIERS)})"
        )

    return {name: _parse_tier(name, data[name], policy=policy) for name in TIERS}


def load_default_tiers(*, policy: ModelPolicy = DEFAULT_POLICY) -> dict[str, TierCatalogue]:
    """Use ~/.lazytools/model_tiers.toml when present, otherwise packaged defaults.

    A malformed user catalogue fails visibly; it never silently changes the ladder.
    """
    override = Path.home() / ".lazytools" / "model_tiers.toml"
    if override.exists():
        return load_tiers(override, policy=policy)
    with as_file(files("lazytools.routing").joinpath("default_tiers.toml")) as packaged:
        return load_tiers(packaged, policy=policy)
