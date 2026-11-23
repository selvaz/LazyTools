from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from lazytools.code_bridge.profiles import (
    PolicyError,
    ProfileDocument,
    TaskGrant,
    compile_claude,
    compile_codex,
    load_profiles,
    resolve,
)

ALL_CLASSES = ["read", "edit", "shell"]
EXPECTED = {"read": {"read"}, "edit": {"read", "edit"}, "edit-and-run": set(ALL_CLASSES)}


def grant(tmp_path: Path, network: str = "off", placement: str = "local") -> TaskGrant:
    return TaskGrant(
        tmp_path,
        {
            "tool_classes": ALL_CLASSES,
            "tools": ["mcp", "web"],
            "network_routes": ["https://example.test"] if network == "on" else [],
            "mcp_servers": {"approved": {"tools": ["lookup"]}},
            "mcp_inventory": {
                "approved": {"command": "approved-server", "tools": ["lookup", "mutate"]},
                "rogue": {"command": "rogue-server", "tools": ["escape"]},
            },
        },
        network,
        placement,
    )


@pytest.mark.parametrize("profile", EXPECTED)
@pytest.mark.parametrize("network", ["off", "on"])
@pytest.mark.parametrize("placement", ["local", "cloud"])
def test_all_profile_switch_combinations(tmp_path: Path, profile: str, network: str, placement: str) -> None:
    policy = resolve(profile, grant(tmp_path, network, placement))
    assert policy.tool_classes == EXPECTED[profile]
    assert policy.folder == tmp_path.resolve()
    assert policy.network == network
    assert policy.placement == placement
    assert set(policy.mcp_servers) == ({"approved"} if network == "on" else set())
    codex = compile_codex(policy)
    assert codex["config_overrides"]["sandbox_workspace_write"]["network_access"] is (network == "on")
    assert codex["config_overrides"]["web_search"] is (network == "on")
    assert "rogue" not in json.dumps(compile_claude(policy))
    assert "rogue" not in json.dumps(codex)


@pytest.mark.parametrize("network,placement", [("off", "local"), ("off", "cloud"), ("on", "local"), ("on", "cloud")])
def test_edit_never_gets_shell(tmp_path: Path, network: str, placement: str) -> None:
    policy = resolve("edit", grant(tmp_path, network, placement))
    assert "shell" not in policy.tool_classes
    assert compile_codex(policy)["config_overrides"]["shell_execution"] is False


@pytest.mark.parametrize(("profile", "tool"), [("edit", "shell"), ("edit", "Bash"), ("read", "edit"), ("read", "shell")])
def test_native_tools_cannot_bypass_profile_ceiling(tmp_path: Path, profile: str, tool: str) -> None:
    with pytest.raises(PolicyError, match="unknown tool grant"):
        resolve(profile, TaskGrant(tmp_path, {"tools": [tool]}, "on", "local"))


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda g: ("missing", g), "unknown profile"),
        (lambda g: ("read", TaskGrant(g.folder, {**g.grants, "surprise": True}, "off", "local")), "unknown grant"),
        (lambda g: ("read", TaskGrant(g.folder, g.grants, "maybe", "local")), "unknown network"),
        (lambda g: ("read", TaskGrant(g.folder, g.grants, "off", "mars")), "unknown placement"),
    ],
)
def test_unknown_profile_key_and_switch_values_are_rejected(tmp_path: Path, mutation, match: str) -> None:
    profile, changed = mutation(grant(tmp_path))
    with pytest.raises(PolicyError, match=match):
        resolve(profile, changed)


def test_schema_unknown_key_and_value_are_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('version=1\nrevision="r"\nextra=true\n[profiles.read]\nallowed_tool_classes=["read"]\n')
    with pytest.raises(PolicyError, match="unknown top-level"):
        load_profiles(bad)
    bad.write_text('version=1\nrevision="r"\n[profiles.read]\nallowed_tool_classes=["teleport"]\n')
    with pytest.raises(PolicyError, match="unknown tool class"):
        load_profiles(bad)


def test_policy_is_frozen(tmp_path: Path) -> None:
    policy = resolve("read", grant(tmp_path))
    with pytest.raises(FrozenInstanceError):
        policy.network = "on"  # type: ignore[misc]
    with pytest.raises(TypeError):
        policy.mcp_servers["rogue"] = {}  # type: ignore[index]


def test_digest_changes_with_revision(tmp_path: Path) -> None:
    original = load_profiles()
    changed = ProfileDocument(original.version, original.revision + ".new", original.profiles)
    assert resolve("read", grant(tmp_path), original).digest != resolve("read", grant(tmp_path), changed).digest


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_compiler_golden(engine: str) -> None:
    workspace = Path("/workspace/example-task")
    policy = resolve("edit-and-run", grant(workspace, "on", "cloud"))
    actual = compile_claude(policy) if engine == "claude" else compile_codex(policy)
    golden = Path(__file__).with_name("fixtures") / f"code_bridge_profiles_{engine}.json"
    assert actual == json.loads(golden.read_text())
