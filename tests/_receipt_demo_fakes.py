"""Offline adapters for the receipt demo in a source checkout."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import lazybridge

from _code_bridge_fakes import FakeAgent, FakeEngine, Script
from code_bridge_test_helpers import FAKE_GH, PUBLICATION_REMOTE
from lazytools.code_bridge import _cloud, _engines, _gh, _routing
from lazytools.projects.admission import TelemetryReading, WindowReading


def install(
    stack: ExitStack, state: Path, repo: Path, base: str, source_file: str, test_file: str,
    *, writer_script: Script | None = None, writer_commit: bool = False,
    review_accepted: bool | Callable[[], bool] = True,
) -> Path:
    stack.enter_context(patch.dict(os.environ, {
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "0",
        "CODEX_BIN": sys.executable,
        "FAKE_GH_STATE": str(state / "fake-github.json"),
        "FAKE_GH_BARE": str(state / "fake-remote.git"),
        "FAKE_GH_DEFAULT_BRANCH": base,
        "FAKE_GH_DEFAULT_BRANCH_MISSING": "",
        "FAKE_GH_HEAD": "",
        "FAKE_GH_NON_DRAFT": "",
    }))
    git = _cloud._git
    checkout = state / "checkout"
    bare = state / "fake-remote.git"
    git(state, "clone", "--template=", "--no-tags", "--", str(repo), str(checkout))
    git(checkout, "config", "user.name", "Receipt Demo")
    git(checkout, "config", "user.email", "receipt-demo@example.invalid")
    git(checkout, "remote", "set-url", "origin", PUBLICATION_REMOTE)
    git(state, "clone", "--template=", "--bare", "--shared", "--no-tags", "--", str(checkout), str(bare))
    git(bare, "update-ref", f"refs/heads/{base}", git(checkout, "rev-parse", "HEAD"))
    gh = state / "fake-gh.py"
    gh.write_text(FAKE_GH, encoding="utf-8")

    def local_git(cwd: Path, *args: str, **kwargs: Any) -> str:
        if args[:3] == ("-c", "push.followTags=false", "push"):
            if args[3:5] != ("--", PUBLICATION_REMOTE) or len(args) != 6:
                raise AssertionError(f"Unexpected publication push: {args}")
            sha, ref = args[5].split(":", 1)
            git(bare, "update-ref", ref, sha, "0" * len(sha))
            return ""
        return git(cwd, *args, **kwargs)

    now = datetime.now(UTC)
    readings = {
        "codex": TelemetryReading("codex", "fake quota", now, (
            WindowReading("codex/10080m", 10, 10080, now + timedelta(days=7)),
            WindowReading("codex/300m", 10, 300, now + timedelta(hours=5)),
        )),
        "claude_code": TelemetryReading("claude_code", "fake quota", now, (
            WindowReading("weekly/all models", 10, 10080, now + timedelta(days=7)),
            WindowReading("session", 10, 300, now + timedelta(hours=5)),
        )),
    }
    script = writer_script if writer_script is not None else Script()
    writer_requests_approval = script.request_approval

    class DemoAgent(FakeAgent):
        async def run(self, prompt: str) -> Any:
            review = prompt.startswith("Review commit ")
            script.request_approval = writer_requests_approval and not review
            if review:
                accepted = review_accepted() if callable(review_accepted) else review_accepted
                script.text = json.dumps({
                    "verdict": "accepted" if accepted else "blocked", "sha": prompt.split()[2],
                    "reason": ("Subtraction and its test match the brief." if accepted else "A required check failed."),
                })
            else:
                source = checkout / source_file
                content = source.read_text(encoding="utf-8") + "\n# Revision\n" if source.exists() else (
                    "def sub(a, b):\n    return a - b\n"
                )
                source.write_text(content, encoding="utf-8")
                (checkout / test_file).write_text(
                    f"import unittest\n\nfrom {Path(source_file).stem} import sub\n\n\n"
                    "class SubTests(unittest.TestCase):\n"
                    "    def test_sub(self):\n"
                    "        self.assertEqual(sub(2, 5), -3)\n",
                    encoding="utf-8",
                )
            result = await super().run(prompt)
            if writer_commit and not review and result.ok:
                git(checkout, "add", "--", source_file, test_file)
                git(checkout, "commit", "-m", "Add subtraction and its test")
            return result

    DemoAgent.script = script
    stack.enter_context(patch.object(lazybridge, "Agent", DemoAgent))
    stack.enter_context(patch.object(_engines, "_make_codex_engine", lambda **kwargs: FakeEngine("codex", script, kwargs)))
    stack.enter_context(patch.object(_engines, "_make_claude_engine", lambda **kwargs: FakeEngine("claude", script, kwargs)))
    stack.enter_context(patch.object(_routing, "read_readings", lambda: readings))
    stack.enter_context(patch.object(_cloud, "_git", local_git))
    stack.enter_context(patch.object(_gh, "gh_argv", lambda: [sys.executable, str(gh)]))
    return checkout
