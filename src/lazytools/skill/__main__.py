"""Install the bundled LazyTools skill for Claude Code and Codex."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lazytools.skill import skill_path

HOMES = {"claude": Path.home() / ".claude", "codex": Path.home() / ".codex"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lazytools.skill")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("install", help="install the LazyTools and WizengAImot skills")
    cmd.add_argument("--to", choices=["claude", "codex", "all"], help="default: every assistant found")
    cmd.add_argument("--dest", type=Path, help="install into this folder instead")
    cmd.add_argument("--force", action="store_true", help="replace a foreign folder")
    args = parser.parse_args(argv)

    try:
        from lazybridge.skill import install_skill
    except ImportError:
        print("Upgrade lazybridge to 1.8.7 or newer to install this skill.", file=sys.stderr)
        return 1

    if args.dest:
        targets = [args.dest]
    else:
        names = (
            list(HOMES) if args.to == "all" else [args.to] if args.to else [n for n, h in HOMES.items() if h.is_dir()]
        )
        targets = [HOMES[n] / "skills" / "lazytools" for n in names or ["claude"]]
    if any(dest.name == "wizengaimot" for dest in targets):
        print("refused: LazyTools and WizengAImot destinations must be different", file=sys.stderr)
        return 1
    try:
        for dest in targets:
            print(f"installed {install_skill(dest, source=skill_path(), force=args.force)}")
            council_dest = dest.parent / "wizengaimot"
            print(f"installed {install_skill(council_dest, source=skill_path() / 'wizengaimot', force=args.force)}")
    except FileExistsError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
