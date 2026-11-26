from pathlib import Path

from tools.capabilities import bridge_subcommands, catalog_entries

CATALOG = Path(__file__).resolve().parents[1] / "docs" / "CAPABILITIES.md"


def test_every_code_bridge_subcommand_is_in_catalog_call_column():
    entries = catalog_entries(CATALOG)
    calls = "\n".join(entry["call"] for entry in entries)
    missing = [command for command in bridge_subcommands() if command not in calls]

    assert not missing, f"Code bridge subcommands missing from catalog call column: {missing}"
