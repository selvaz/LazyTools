"""CLI for daily ecosystem evidence and dated realignment plans."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .check import DEFAULT_ROOT, Checker, command, exit_code, markdown


def default_manifest():
    source = Path(__file__).resolve().parents[3] / "ecosystem" / "compatibility.toml"
    return source if source.exists() else Path(__file__).with_name("compatibility.toml")


def default_out_dir():
    return Path("D:/ecosystem-sync") if Path("D:/").exists() else Path.home() / ".lazytools" / "ecosystem-sync"


def notify(summary, *, client=None):
    from lazytools.connectors.telegram.client import TelegramClient

    chat = os.environ["TELEGRAM_CHAT_ID"]
    client = client or TelegramClient.from_token(os.environ["TELEGRAM_BOT_TOKEN"], timeout=30)
    if len(summary) > 4000:
        client.send_document(chat_id=chat, document=summary.encode("utf-8"), filename="ecosystem-sync-summary.txt")
    else:
        client.send_message(chat_id=chat, text=summary)


def write_plan(plan, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = plan["generated_at"][:10] + "-plan"
    md = markdown(plan)
    (out_dir / (stem + ".json")).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / (stem + ".md")).write_text(md, encoding="utf-8")
    return md


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only daily ecosystem realignment plan; never applies actions")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Inspect Git/GitHub, worktrees, production audit and disk")
    check.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    check.add_argument("--manifest", type=Path, default=default_manifest())
    check.add_argument("--out-dir", type=Path, default=default_out_dir())
    check.add_argument("--no-fetch", action="store_true")
    check.add_argument("--stale-days", type=int, default=7)
    check.add_argument("--telegram", action="store_true", help="Opt in to sending the 10-line summary")
    args = parser.parse_args(argv)
    if args.stale_days < 0:
        parser.error("--stale-days must be nonnegative")
    # Dates follow the operator's local day; all age calculations use aware times.
    now = datetime.now(UTC).astimezone()
    try:
        plan = Checker(
            args.root.resolve(), args.manifest.resolve(), fetch=not args.no_fetch, stale_days=args.stale_days, now=now
        ).check()
    except Exception as exc:
        fact = f"Check initialization failed: {type(exc).__name__}: {exc}"
        plan = {
            "schema_version": 1,
            "generated_at": now.isoformat(),
            "root": str(args.root),
            "manifest": str(args.manifest),
            "fetch": not args.no_fetch,
            "stale_days": args.stale_days,
            "actions": [
                {
                    "class": "session",
                    "repo": "ecosystem",
                    "fact": fact,
                    "command": command("lazytools-ecosystem-sync", "check", "--root", args.root, "--no-fetch"),
                    "risk": "Evidence incomplete",
                }
            ],
            "observations": [],
            "errors": [{"repo": "ecosystem", "error": fact}],
            "repositories": [],
            "worktrees": [],
            "pull_requests": [],
            "production_audit": {},
        }
    try:
        md = write_plan(plan, args.out_dir)
        if args.telegram:
            try:
                notify("\n".join(md.splitlines()[:10]))
            except Exception as exc:
                # Never include credentials or arbitrary transport error text.
                fact = f"Telegram summary delivery failed ({type(exc).__name__})"
                plan["errors"].append({"repo": "telegram", "error": fact})
                plan["actions"].append(
                    {
                        "class": "session",
                        "repo": "telegram",
                        "fact": fact,
                        "command": command("Get-Item", "Env:TELEGRAM_CHAT_ID"),
                        "risk": "Check connector configuration and retry notification",
                    }
                )
                md = write_plan(plan, args.out_dir)
        print("\n".join(md.splitlines()[:10]))
        print(f"Plans: {args.out_dir / (now.date().isoformat() + '-plan.md')} and .json")
    except OSError as exc:
        print(f"Cannot write plan: {exc}", file=sys.stderr)
        return 1
    return exit_code(plan)


if __name__ == "__main__":
    sys.exit(main())
