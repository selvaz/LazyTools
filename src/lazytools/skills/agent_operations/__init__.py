"""Bundled ``agent-operations`` skill: running software work with coding agents.

A portable assistant skill (Claude Code, Codex, or any host that loads
``SKILL.md`` folders) for the lead that delegates work through the LazyTools
async code bridge, quota-aware routing and the projects layer.

Layout (progressive disclosure)::

    lazytools/skills/agent_operations/
        SKILL.md              # index: roles, the loop, golden rules, routing table
        references/*.md       # one file per area, read only when the task needs it

Install the whole directory, not just ``SKILL.md``; the index routes to the
reference files by relative path::

    python -m lazytools.skills.agent_operations install

The same folder is registered as the packaged documentation skill
``agent-operations`` (see :func:`lazytools.skills.build_packaged_skill`).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["SKILL_NAME", "install_skill", "reference_paths", "skill_path"]

#: Name in the ``SKILL.md`` frontmatter and the default install folder name.
SKILL_NAME = "agent-operations"


def skill_path() -> Path:
    """Return the on-disk directory containing ``SKILL.md`` and ``references/``."""
    return Path(__file__).resolve().parent


def reference_paths() -> list[Path]:
    """Return the bundled reference files (``references/*.md``), sorted by name."""
    ref_dir = skill_path() / "references"
    return sorted(ref_dir.glob("*.md")) if ref_dir.is_dir() else []


def install_skill(dest: Path, *, force: bool = False) -> Path:
    """Copy this skill folder into ``dest`` and return ``dest``.

    Reuses LazyBridge's installer: a copy (no symlink), Python files left out,
    an earlier install replaced, and any other folder at ``dest`` refused
    with ``FileExistsError`` unless ``force`` is true.
    """
    from lazybridge.skill import install_skill as _install

    return _install(Path(dest), source=skill_path(), force=force)
