import argparse
from pathlib import Path

from tools.capabilities import bridge_subcommands, catalog_entries

from lazytools.code_bridge import cli

CATALOG = Path(__file__).resolve().parents[1] / "docs" / "CAPABILITIES.md"


def test_catalog_entries_reads_capabilities_and_providers():
    entries = catalog_entries(CATALOG)
    assert {
        "section": "1. Coding agents: code bridge (`lazytools-code-bridge`)",
        "capability": "Run a local coding job",
        "call": (
            "`lazytools-code-bridge run --engine codex|claude --cwd REPO --task @brief.md "
            "[--tier basic|writing|thinking|critical] [--session ALIAS] [--detach]`"
        ),
        "status": "live-tested",
    } in entries
    assert {
        "section": "8. MCP tools by provider (`lazytools-mcp`)",
        "capability": "`codemap`",
        "call": "`codemap_map`, `codemap_search`",
        "status": "tests-only",
    } in entries


def test_catalog_entries_skips_other_tables_and_accepts_alignment_markers(tmp_path):
    catalog = tmp_path / "CAPABILITIES.md"
    catalog.write_text(
        "## Commands\n"
        "| Capability | How to call | Status |\n"
        "| :--- | :---: | ---: |\n"
        "| Résumé | `run a\\|b` | tests-only |\n"
        "| Publish | `publish` | live-tested (on branch) |\n"
        "\n"
        "| You want | Use | Notes |\n"
        "| --- | --- | --- |\n"
        "| Quick lookup | `run` | Example |\n",
        encoding="utf-8",
    )

    assert catalog_entries(str(catalog)) == [
        {"section": "Commands", "capability": "Résumé", "call": "`run a|b`", "status": "tests-only"},
        {"section": "Commands", "capability": "Publish", "call": "`publish`", "status": "live-tested (on branch)"},
    ]


def test_bridge_subcommands_lists_top_level_commands():
    commands = bridge_subcommands()
    assert commands == sorted(set(commands))
    assert {"cloud-preflight", "dispatch", "navigate", "run", "watch"} <= set(commands)
    assert "probe" not in commands


def test_bridge_subcommands_uses_current_parser_choices(monkeypatch):
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["text", "json"])
    subcommands = parser.add_subparsers()
    subcommands.add_parser("zeta")
    subcommands.add_parser("alpha")
    monkeypatch.setattr(cli, "build_parser", lambda: parser)

    assert bridge_subcommands() == ["alpha", "zeta"]
