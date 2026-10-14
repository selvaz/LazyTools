"""Deterministic, quota-aware model selection and a bounded live adapter."""

from lazytools.routing.catalogue import (
    TIERS,
    ModelTiersError,
    StepModel,
    TierCatalogue,
    TierStep,
    load_default_tiers,
    load_tiers,
)
from lazytools.routing.live import recommend
from lazytools.routing.policy import DEFAULT_POLICY, ModelPolicy
from lazytools.routing.router import (
    CapabilityRequirement,
    ContinuityHint,
    ProviderScore,
    RoutingDecision,
    WindowAvailability,
    route,
)

__all__ = [
    "TIERS",
    "ModelTiersError",
    "StepModel",
    "TierCatalogue",
    "TierStep",
    "load_default_tiers",
    "load_tiers",
    "recommend",
    "DEFAULT_POLICY",
    "ModelPolicy",
    "CapabilityRequirement",
    "ContinuityHint",
    "ProviderScore",
    "RoutingDecision",
    "WindowAvailability",
    "route",
]
