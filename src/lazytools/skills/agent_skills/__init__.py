"""Agent skills shipped with LazyTools: ``SKILL.md`` guides an LLM assistant follows.

Unlike the BM25 documentation bundles of :mod:`lazytools.skills.doc_skills`,
these are hand-written instruction files in the common ``SKILL.md`` format
(YAML front matter with ``name`` and ``description``, then Markdown). Claude
Code loads them from ``~/.claude/skills/<name>/SKILL.md`` or a project's
``.claude/skills/<name>/SKILL.md``; other assistants can be pointed at the
file directly.

    from lazytools.skills.agent_skills import agent_skill_path, install_agent_skill
    install_agent_skill("code-bridge-cloud-setup")   # -> ~/.claude/skills/code-bridge-cloud-setup/SKILL.md

The same is available as ``lazytools-code-bridge setup-skill [--install [DIR]]``.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_INSTALL_DIR = Path("~/.claude/skills")


def list_agent_skills() -> list[str]:
    """Names of the bundled agent skills."""
    return sorted(p.parent.name for p in ROOT.glob("*/SKILL.md"))


def agent_skill_path(name: str) -> Path:
    """Path of a bundled skill's ``SKILL.md``; ``KeyError`` for an unknown name."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or not (ROOT / name / "SKILL.md").is_file():
        raise KeyError(f"no bundled agent skill {name!r} (one of {', '.join(list_agent_skills())})")
    return ROOT / name / "SKILL.md"


def skill_metadata(name: str) -> dict[str, str]:
    """The ``name`` and ``description`` from a skill's front matter."""
    text = agent_skill_path(name).read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, flags=re.DOTALL)
    meta: dict[str, str] = {}
    for line in (match.group(1).splitlines() if match else []):
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta


def install_agent_skill(name: str, dest: Path | None = None, *, overwrite: bool = False) -> Path:
    """Copy a bundled skill to ``dest/<name>/SKILL.md`` (default ``~/.claude/skills``); return the new path."""
    source = agent_skill_path(name)
    target_dir = (dest or DEFAULT_INSTALL_DIR).expanduser() / name
    target = target_dir / "SKILL.md"
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists (pass overwrite=True / --force to replace it)")
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


__all__ = ["DEFAULT_INSTALL_DIR", "agent_skill_path", "install_agent_skill", "list_agent_skills", "skill_metadata"]
