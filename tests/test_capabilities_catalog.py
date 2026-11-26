import re
from pathlib import Path

from lazytools.code_bridge._capabilities import bridge_subcommands, catalog_entries

CATALOG = Path(__file__).resolve().parents[1] / "docs" / "CAPABILITIES.md"
# A documented command appears as `lazytools-code-bridge <subcommand>` in a call cell,
# possibly after `|` alternatives such as `lazytools-code-bridge pending | approve <id>`.
_INVOCATION = re.compile(r"lazytools-code-bridge\s+([a-z][a-z-]*)((?:\s*[|/,]\s*[a-z][a-z-]*)*)")


def documented_subcommands(calls: list[str]) -> set[str]:
    found: set[str] = set()
    for call in calls:
        for match in _INVOCATION.finditer(call):
            found.add(match.group(1))
            found.update(re.findall(r"[a-z][a-z-]*", match.group(2)))
    return found


def test_every_code_bridge_subcommand_is_in_catalog_call_column():
    documented = documented_subcommands([entry["call"] for entry in catalog_entries(CATALOG)])
    missing = [command for command in bridge_subcommands() if command not in documented]

    assert not missing, f"Code bridge subcommands missing from catalog call column: {missing}"


def test_a_substring_of_another_tool_does_not_count_as_documentation():
    assert "models" not in documented_subcommands(["`regime_compare_emission_models`"])
    assert documented_subcommands(["`lazytools-code-bridge models [--json]`"]) == {"models"}
