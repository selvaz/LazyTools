"""Install the bundled ``agent-operations`` skill for Claude Code and Codex.

    python -m lazytools.skills.agent_operations install            # every assistant found
    python -m lazytools.skills.agent_operations install --to codex
    python -m lazytools.skills.agent_operations install --dest DIR # any folder

It copies the skill folder into ``~/.claude/skills/agent-operations`` and/or
``~/.codex/skills/agent-operations``. A folder it installed before is
replaced; anything else at that path is left alone unless ``--force`` is
given. Rerun it after upgrading LazyTools.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lazytools.skills.agent_operations import SKILL_NAME, install_skill

__all__ = ["main", "homes"]


def homes() -> dict[str, Path]:
    """Assistant home folders, resolved at call time (tests patch ``Path.home``)."""
    return {"claude": Path.home() / ".claude", "codex": Path.home() / ".codex"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lazytools.skills.agent_operations")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("install", help="install the skill for Claude Code and/or Codex")
    cmd.add_argument("--to", choices=["claude", "codex", "all"], help="default: every assistant found")
    cmd.add_argument("--dest", type=Path, help="install into this folder instead")
    cmd.add_argument("--force", action="store_true", help="replace a folder this command did not install")
    args = parser.parse_args(argv)

    if args.dest:
        targets = [args.dest]
    else:
        known = homes()
        if args.to == "all":
            names = list(known)
        elif args.to:
            names = [args.to]
        else:
            names = [name for name, home in known.items() if home.is_dir()] or ["claude"]
        targets = [known[name] / "skills" / SKILL_NAME for name in names]
    try:
        for dest in targets:
            print(f"installed {install_skill(dest, force=args.force)}")
    except FileExistsError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
