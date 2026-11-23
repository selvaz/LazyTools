"""Versioned execution profiles and engine-settings compilation."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

TOOL_CLASSES = frozenset({"read", "edit", "shell"})
TOOL_GRANTS = frozenset({"web", "mcp"})
NETWORK_VALUES = frozenset({"on", "off"})
PLACEMENT_VALUES = frozenset({"local", "cloud"})
GRANT_KEYS = frozenset({"tool_classes", "tools", "mcp_servers", "mcp_inventory", "network_routes", "scratch_folder"})
DEFAULT_PROFILE_PATH = Path(__file__).with_name("data") / "profiles.example.toml"


class PolicyError(ValueError):
    """Raised when a profile document or task grant is invalid."""


@dataclass(frozen=True)
class Profile:
    name: str
    allowed_tool_classes: frozenset[str]


@dataclass(frozen=True)
class ProfileDocument:
    version: int
    revision: str
    profiles: Mapping[str, Profile]


@dataclass(frozen=True)
class TaskGrant:
    folder: Path | str
    grants: Mapping[str, Any]
    network: str
    placement: str


@dataclass(frozen=True)
class EffectivePolicy:
    profile: str
    version: int
    revision: str
    folder: Path
    scratch_folder: Path
    tool_classes: frozenset[str]
    tools: frozenset[str]
    network: str
    network_routes: tuple[str, ...]
    placement: str
    mcp_servers: Mapping[str, Mapping[str, Any]]
    mcp_inventory: tuple[str, ...]
    digest: str


def _expect_keys(value: Mapping[str, Any], allowed: set[str] | frozenset[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PolicyError(f"unknown {where} key(s): {', '.join(unknown)}")


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple | set | frozenset) or any(not isinstance(item, str) for item in value):
        raise PolicyError(f"{where} must be a list of strings")
    return tuple(value)


def load_profiles(path: Path | str = DEFAULT_PROFILE_PATH) -> ProfileDocument:
    """Load a strictly validated profile document from *path*."""
    try:
        with Path(path).open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"cannot load profile document: {exc}") from exc
    _expect_keys(raw, {"version", "revision", "profiles"}, "top-level")
    if not isinstance(raw.get("version"), int) or isinstance(raw.get("version"), bool):
        raise PolicyError("version must be an integer")
    if raw["version"] != 1:
        raise PolicyError(f"unsupported profile schema version: {raw['version']!r}")
    if not isinstance(raw.get("revision"), str) or not raw["revision"]:
        raise PolicyError("revision must be a non-empty string")
    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise PolicyError("profiles must be a non-empty table")
    profiles: dict[str, Profile] = {}
    for name, value in raw_profiles.items():
        if not isinstance(value, dict):
            raise PolicyError(f"profile {name!r} must be a table")
        _expect_keys(value, {"allowed_tool_classes"}, f"profile {name!r}")
        classes = frozenset(_strings(value.get("allowed_tool_classes"), f"profile {name!r}.allowed_tool_classes"))
        unknown = classes - TOOL_CLASSES
        if unknown:
            raise PolicyError(f"unknown tool class(es) in profile {name!r}: {', '.join(sorted(unknown))}")
        profiles[name] = Profile(name, classes)
    return ProfileDocument(raw["version"], raw["revision"], MappingProxyType(profiles))


def _normalise_servers(grants: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    requested = grants.get("mcp_servers", {})
    inventory = grants.get("mcp_inventory", requested)
    if not isinstance(requested, dict) or not isinstance(inventory, dict):
        raise PolicyError("mcp_servers and mcp_inventory must be tables")
    if any(not isinstance(name, str) or not name or not isinstance(value, dict) for name, value in inventory.items()):
        raise PolicyError("each mcp_inventory entry must be a named table")
    result: dict[str, Mapping[str, Any]] = {}
    for name, request in requested.items():
        if not isinstance(name, str) or not isinstance(request, dict):
            raise PolicyError("each mcp_servers entry must be a named table")
        _expect_keys(request, {"tools"}, f"MCP grant {name!r}")
        granted_tools = set(_strings(request.get("tools"), f"MCP grant {name!r}.tools"))
        source = inventory.get(name)
        if not isinstance(source, dict):
            raise PolicyError(f"granted MCP server {name!r} is absent from the inventory")
        available_tools = set(_strings(source.get("tools", ()), f"MCP inventory {name!r}.tools"))
        unknown_tools = granted_tools - available_tools
        if unknown_tools:
            raise PolicyError(
                f"MCP server {name!r} does not provide granted tool(s): {', '.join(sorted(unknown_tools))}"
            )
        config = {key: value for key, value in source.items() if key != "tools"}
        config["tools"] = sorted(granted_tools)
        result[name] = MappingProxyType(config)
    return MappingProxyType(result)


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def resolve(profile: str, grant: TaskGrant, document: ProfileDocument | None = None) -> EffectivePolicy:
    """Intersect explicit task grants with the named profile ceiling."""
    document = document or load_profiles()
    if profile not in document.profiles:
        raise PolicyError(f"unknown profile: {profile!r}")
    if grant.network not in NETWORK_VALUES:
        raise PolicyError(f"unknown network setting: {grant.network!r}")
    if grant.placement not in PLACEMENT_VALUES:
        raise PolicyError(f"unknown placement setting: {grant.placement!r}")
    if not isinstance(grant.grants, Mapping):
        raise PolicyError("grants must be a mapping")
    _expect_keys(grant.grants, GRANT_KEYS, "grant")
    folder = Path(grant.folder).expanduser().resolve()
    scratch = Path(grant.grants.get("scratch_folder", folder / ".scratch")).expanduser()
    if not scratch.is_absolute():
        scratch = folder / scratch
    scratch = scratch.resolve()
    if scratch != folder and folder not in scratch.parents:
        raise PolicyError("scratch_folder must be inside the assigned folder")
    requested_classes = frozenset(_strings(grant.grants.get("tool_classes", ()), "tool_classes"))
    unknown_classes = requested_classes - TOOL_CLASSES
    if unknown_classes:
        raise PolicyError(f"unknown granted tool class(es): {', '.join(sorted(unknown_classes))}")
    classes = requested_classes & document.profiles[profile].allowed_tool_classes
    tools = frozenset(_strings(grant.grants.get("tools", ()), "tools"))
    unknown_tools = tools - TOOL_GRANTS
    if unknown_tools:
        raise PolicyError(f"unknown tool grant(s): {', '.join(sorted(unknown_tools))}")
    routes = _strings(grant.grants.get("network_routes", ()), "network_routes")
    if grant.network == "off" and routes:
        raise PolicyError("network_routes require network='on'")
    servers = _normalise_servers(grant.grants)
    inventory = tuple(sorted(grant.grants.get("mcp_inventory", grant.grants.get("mcp_servers", {}))))
    if servers and "mcp" not in tools:
        raise PolicyError("MCP servers require the explicit 'mcp' tool grant")
    # Native web and tool hosts are task-controlled egress surfaces.  They are
    # removed, rather than merely annotated, so an engine with incomplete
    # network sandboxing cannot accidentally expose them in an offline task.
    if grant.network == "off":
        tools = tools - {"web", "mcp"}
        servers = MappingProxyType({})
    canonical = {
        "profile": profile,
        "version": document.version,
        "revision": document.revision,
        "folder": str(folder),
        "scratch_folder": str(scratch),
        "tool_classes": sorted(classes),
        "tools": sorted(tools),
        "network": grant.network,
        "network_routes": list(routes),
        "placement": grant.placement,
        "mcp_servers": {name: dict(config) for name, config in sorted(servers.items())},
        "mcp_inventory": inventory,
    }
    return EffectivePolicy(
        profile,
        document.version,
        document.revision,
        folder,
        scratch,
        classes,
        tools,
        grant.network,
        routes,
        grant.placement,
        servers,
        inventory,
        _digest(canonical),
    )


def _metadata(policy: EffectivePolicy) -> dict[str, str | int]:
    return {"policy_version": policy.version, "policy_revision": policy.revision, "policy_digest": policy.digest}


def compile_claude(policy: EffectivePolicy) -> dict[str, Any]:
    """Compile isolated Claude settings and a strict MCP allowlist."""
    servers = {
        name: {key: value for key, value in config.items() if key != "tools"}
        for name, config in sorted(policy.mcp_servers.items())
    }
    return {
        **_metadata(policy),
        "settings": {
            "settingSources": [],
            "permissions": {
                "allow": sorted(policy.tools | policy.tool_classes),
                "deny": ["*"],
                "additionalDirectories": [],
            },
            "workspace": str(policy.folder),
            "network": {"enabled": policy.network == "on", "allowedRoutes": list(policy.network_routes)},
        },
        "mcp_config": {
            "strict": True,
            "mcpServers": servers,
            "allowedTools": {name: list(config["tools"]) for name, config in sorted(policy.mcp_servers.items())},
        },
    }


def compile_codex(policy: EffectivePolicy) -> dict[str, Any]:
    """Generate a session CODEX_HOME config; admission requires launch verification."""
    writable = "edit" in policy.tool_classes
    overrides = {
        "sandbox_mode": "workspace-write" if writable else "read-only",
        "approval_policy": "never",
        "sandbox_workspace_write": {
            "network_access": policy.network == "on",
            "writable_roots": [str(policy.folder), str(policy.scratch_folder)] if writable else [],
        },
        "tools": {"web_search": policy.network == "on" and "web" in policy.tools},
        "mcp_servers": {name: {"enabled": name in policy.mcp_servers} for name in policy.mcp_inventory},
    }
    reasons = ["The launcher has not verified the isolated CODEX_HOME and excluded project configuration."]
    if "shell" not in policy.tool_classes:
        reasons.append("Codex shell denial is not enforced by this configuration.")
    else:
        reasons.append("Scoped shell commands are not enforced by this configuration.")
    if policy.mcp_servers:
        reasons.append("MCP server definitions and per-tool restrictions are not enforced by this configuration.")
    if policy.network_routes:
        reasons.append("Network route restrictions are not enforced by this configuration.")
    quote = lambda value: json.dumps(value, ensure_ascii=True)
    lines = [f'sandbox_mode = {quote(overrides["sandbox_mode"])}', 'approval_policy = "never"',
             "[sandbox_workspace_write]", f'network_access = {str(policy.network == "on").lower()}',
             f'writable_roots = {quote(overrides["sandbox_workspace_write"]["writable_roots"])}',
             "[tools]", f'web_search = {str(overrides["tools"]["web_search"]).lower()}']
    for name, config in overrides["mcp_servers"].items():
        lines.extend([f'[mcp_servers.{quote(name)}]', f'enabled = {str(config["enabled"]).lower()}'])
    return {
        **_metadata(policy),
        "eligible": False,
        "ineligibility_reasons": reasons,
        "codex_home_config_toml": "\n".join(lines) + "\n",
        "config_overrides": overrides,
    }
