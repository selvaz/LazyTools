"""Resolve local bridge requests and bind native launches to core policy."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lazybridge.policy import (
    EffectivePolicy,
    PolicyError,
    TaskGrant,
    child_within_parent,
    compile_claude,
    compile_codex,
    current_policy_context,
    load_profiles,
    resolve,
)

LEGACY_PROFILES = {"default": "edit-and-run", "review": "read"}
PROFILES = ("default", "review", "read", "edit", "edit-and-run")
MODES = ("auto", "governed", "legacy")
_NATIVE_PREFIX = "code-bridge:native-policy:"
DETACH_PREFIX = "code-bridge:launch:"


@dataclass(frozen=True)
class Launch:
    policy: EffectivePolicy
    compiled: dict[str, Any]
    mode: str
    report: Path | None
    receipt: str | None
    parent_digest: str = ""

    def binding(self) -> dict[str, Any]:
        return {
            "profile": self.policy.profile,
            "network": self.policy.network,
            "placement": self.policy.placement,
            "policy_revision": self.policy.revision,
            "policy_digest": self.policy.digest,
            "launch_mode": self.mode,
            "release_receipt": self.receipt,
        }

    def metadata(self) -> dict[str, Any]:
        return {
            **self.binding(),
            "policy_ineligibility": self.compiled["ineligibility_reasons"],
            "policy_tool_classes": sorted(self.policy.tool_classes),
            "parent_policy_digest": self.parent_digest,
        }


def _compile(
    engine: str,
    policy: EffectivePolicy,
    report: Path | None,
) -> tuple[dict[str, Any], str | None]:
    compiler = compile_codex if engine == "codex" else compile_claude
    if report is None:
        return compiler(policy), None
    if report.is_relative_to(policy.folder):
        raise PolicyError("The release report must be outside the assigned folder")
    if engine == "codex":
        from lazybridge.engines.codex.app_server import codex_executable
        from lazybridge.engines.codex.policy_launch import runtime_revision

        executable = Path(codex_executable()).resolve(strict=True)
        revision = runtime_revision(str(executable))
    else:
        from claude_agent_sdk import ClaudeAgentOptions
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
        from lazybridge.policy.claude_admission import claude_admission_id
        from lazybridge.policy.claude_admission import runtime_revision as claude_revision

        executable = Path(SubprocessCLITransport("", ClaudeAgentOptions())._find_cli()).resolve(strict=True)
        revision = claude_revision(executable)
    if executable.is_relative_to(policy.folder):
        raise PolicyError("The runtime executable must be outside the assigned folder")
    compiled = compiler(policy, release_report=report, runtime_revision=revision)
    receipt = None
    if compiled["eligible"]:
        receipt = compiled["admission_id"] if engine == "codex" else claude_admission_id(policy, report, revision)
    return compiled, receipt


def resolve_launch(
    engine: str,
    cwd: Path,
    profile: str,
    network: str | None,
    mode: str,
    *,
    review: bool = False,
) -> Launch:
    if engine not in {"claude", "codex"} or profile not in PROFILES or mode not in MODES:
        raise PolicyError("Unknown bridge engine, profile, or launch mode")
    name = "read" if review else LEGACY_PROFILES.get(profile, profile)
    profile_file = os.environ.get("LAZYTOOLS_CODE_BRIDGE_PROFILES")
    document = load_profiles(profile_file) if profile_file else load_profiles()
    ceiling = document.profiles.get(name)
    if ceiling is None:
        raise PolicyError(f"Unknown core policy profile {name!r}")
    # Intentional review stays read-only even if a replacement document widens read.
    classes = ("read",) if name == "read" else tuple(ceiling.allowed_tool_classes)
    context = current_policy_context()
    # A budget-only context (tree budget, no policy) carries no authority ceiling.
    parent = context.delegable_grants if context is not None else None
    report_setting = os.environ.get(f"LAZYTOOLS_CODE_BRIDGE_{engine.upper()}_RELEASE_REPORT")
    report = Path(report_setting).expanduser().resolve() if report_setting and mode != "legacy" else None
    # Legacy profile names retain GitHub reads in review and web for writers.
    legacy_input = profile in LEGACY_PROFILES and parent is None and report is None and mode != "governed"
    network = network or ("on" if legacy_input else "off")
    web = network == "on" and name != "read"
    grant = TaskGrant(cwd, {"tool_classes": classes, "tools": ("web",) if web else ()}, network, "local")
    if parent is None:
        policy = resolve(name, grant, document)
    else:
        policy = child_within_parent(parent, grant, name, document)
        if mode == "legacy":
            raise PolicyError("A delegated policy requires a governed launch")

    compiled, receipt = _compile(engine, policy, report)
    if receipt is not None and compiled["eligible"]:
        return Launch(policy, compiled, "governed", report, receipt, parent.digest if parent is not None else "")
    if mode == "legacy" or (mode == "auto" and parent is None and profile in LEGACY_PROFILES and report is None):
        return Launch(policy, compiled, "ungoverned-legacy", None, None)
    raise PolicyError("Governed local launch refused. " + " ".join(compiled["ineligibility_reasons"]))


def check_binding(expected: Any, observed: dict[str, Any]) -> None:
    if expected != observed:
        raise PolicyError("Local launch binding changed (policy revision, grant, receipt, or launch inputs)")


def check_resume(store: Any, engine: str, native_id: str | None, binding: dict[str, Any]) -> None:
    if native_id is None:
        return
    saved = store.read(f"{_NATIVE_PREFIX}{engine}:{native_id}")
    if saved is None:
        if binding.get("launch_mode") != "ungoverned-legacy":
            raise PolicyError("No bridge policy binding for this native session; use a new session alias")
        bind_native(store, engine, native_id, binding)
        return
    check_binding(saved, binding)


def bind_native(store: Any, engine: str, native_id: str, binding: dict[str, Any]) -> None:
    key = f"{_NATIVE_PREFIX}{engine}:{native_id}"
    if not store.compare_and_swap(key, None, binding):
        check_binding(store.read(key), binding)
