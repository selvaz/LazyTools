import re
from pathlib import Path

from tools.capabilities import catalog_entries

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ROOT / "src" / "lazytools" / "mcp_server" / "providers.py"
CATALOG = ROOT / "docs" / "CAPABILITIES.md"


def test_every_mcp_provider_is_in_capabilities_catalog():
    source = PROVIDERS.read_text(encoding="utf-8")
    provider_ids = set(re.findall(r"^@_register\([\"']([^\"']+)[\"']\)", source, re.MULTILINE))
    catalog_ids = {
        entry["capability"].strip("`")
        for entry in catalog_entries(CATALOG)
        if entry["section"] == "8. MCP tools by provider (`lazytools-mcp`)"
    }

    assert provider_ids
    assert provider_ids <= catalog_ids
