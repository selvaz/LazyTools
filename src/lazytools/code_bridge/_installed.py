"""Discovery of coding engines installed for code-bridge review selection."""

from __future__ import annotations

import os
import shutil
import tomllib
from collections.abc import Iterable
from pathlib import Path

from lazytools.code_bridge import _concurrency

ENGINES = frozenset({"codex", "claude"})


def installed_engines(
    override: Iterable[str] | None = None, *, config_path: Path | None = None
) -> frozenset[str]:
    """Return configured or CLI-resolvable engines.

    ``override`` is the deliberately small test/programmatic seam.  When the
    code-bridge config contains ``[engines] installed = [...]``, that explicit
    list wins over executable discovery, including when it is empty.
    """
    if override is not None:
        return _validated(override, "installed engine override")
    path = config_path or _concurrency.config_path()
    if path.exists():
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        engines = data.get("engines")
        if engines is not None:
            if not isinstance(engines, dict) or set(engines) != {"installed"}:
                raise ValueError(f"{path}: [engines] must contain only 'installed'")
            installed = engines["installed"]
            if not isinstance(installed, list) or not all(isinstance(item, str) for item in installed):
                raise ValueError(f"{path}: [engines] installed must be a list of engine names")
            return _validated(installed, f"{path}: [engines] installed")
    found = set()
    for engine, variable, command in (
        ("codex", "CODEX_BIN", "codex"),
        ("claude", "CLAUDE_BIN", "claude"),
    ):
        configured = os.environ.get(variable)
        if (configured and shutil.which(configured)) or (not configured and shutil.which(command)):
            found.add(engine)
    # Discovery that finds no engine is no evidence (CI runners, a PATH the bridge does not
    # see): keep the both-engines behaviour. Only a positive single-engine finding, or an
    # explicit config/override, switches review to the same-provider-fresh fallback.
    return frozenset(found) or ENGINES


def _validated(values: Iterable[str], source: str) -> frozenset[str]:
    result = frozenset(values)
    unknown = result - ENGINES
    if unknown:
        raise ValueError(f"{source}: unknown engines: {sorted(unknown)}")
    return result
