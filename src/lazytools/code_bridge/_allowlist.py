"""Shared strict TOML envelope for capability allow-lists."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def load_rules(source: Path, fields: set[str], *, required: set[str]) -> list[dict[str, Any]]:
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    if set(data) != {"rules"} or not isinstance(data["rules"], list):
        raise ValueError(f"{source}: allow-list must contain a rules array and no unknown top-level keys")
    rules: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in data["rules"]:
        if not isinstance(raw, dict) or set(raw) - fields or required - set(raw):
            raise ValueError(f"{source}: invalid allow-list rule")
        rule_id = raw.get("id")
        if not isinstance(rule_id, str) or not rule_id or rule_id in ids:
            raise ValueError(f"{source}: missing or duplicate rule id {rule_id!r}")
        ids.add(rule_id)
        rules.append(raw)
    return rules
