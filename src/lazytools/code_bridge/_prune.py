"""Find and delete old transcripts of automated coding-engine sessions.

Both engines already record who started a session, so no extra tagging is
needed:

* Claude Code writes ``"entrypoint"`` into every transcript line: ``sdk-py``
  for sessions driven through the Python SDK (LazyBridge, LazyCEO, the
  bridge); ``cli``, ``claude-desktop`` and ``sdk-cli`` (print mode, Remote
  Control) are a person's own.
* Codex writes ``"originator"`` into the rollout's ``session_meta``:
  ``lazybridge`` for app-server sessions LazyBridge opened, ``Codex Desktop`` /
  ``codex_exec`` for a person's own.

Only automated sessions are ever candidates, and only when older than the
cut-off. A transcript whose origin cannot be read is kept, and so is any
session a named alias still points at (deleting it would break ``--session``
resume). The default is a dry run.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: Only the Python SDK (LazyBridge, LazyCEO, this bridge). ``sdk-cli`` is NOT here:
#: Claude Code also uses it for a person's ``claude -p`` and Remote Control sessions.
AUTOMATED_CLAUDE = frozenset({"sdk-py"})
AUTOMATED_CODEX = frozenset({"lazybridge"})

_CHUNK_BYTES = 1024 * 1024
#: The marker sits on the first message line, which can carry a very large prompt
#: (LazyCommittee packets reach several MB), so read in chunks instead of a fixed head.
_SCAN_LIMIT_BYTES = 64 * 1024 * 1024
_OVERLAP = 256
_ENTRYPOINT = re.compile(r'"entrypoint"\s*:\s*"([^"]*)"')
_ORIGINATOR = re.compile(r'"originator"\s*:\s*"([^"]*)"')
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Transcript:
    engine: str  # "claude" or "codex"
    path: Path
    session_id: str
    origin: str | None
    size: int
    mtime: float
    #: Sibling directory holding this session's sub-agent transcripts (Claude only).
    companion: Path | None = None

    @property
    def automated(self) -> bool:
        allowed = AUTOMATED_CLAUDE if self.engine == "claude" else AUTOMATED_CODEX
        return self.origin in allowed

    @property
    def total_size(self) -> int:
        if self.companion is None or not self.companion.is_dir():
            return self.size
        return self.size + sum(f.stat().st_size for f in self.companion.rglob("*") if f.is_file())


def _origin(path: Path, pattern: re.Pattern[str]) -> str | None:
    tail = ""
    read = 0
    try:
        with path.open("rb") as stream:
            while read < _SCAN_LIMIT_BYTES:
                chunk = stream.read(_CHUNK_BYTES)
                if not chunk:
                    return None
                read += len(chunk)
                text = tail + chunk.decode("utf-8", "ignore")
                match = pattern.search(text)
                if match:
                    return match.group(1)
                tail = text[-_OVERLAP:]
    except OSError:
        return None
    return None


def claude_transcripts(root: Path) -> Iterable[Transcript]:
    """``<root>/<project>/<session-id>.jsonl`` (root is ``~/.claude/projects``)."""
    if not root.is_dir():
        return
    for path in root.glob("*/*.jsonl"):
        stat = path.stat()
        companion = path.with_suffix("")
        yield Transcript(
            "claude",
            path,
            path.stem,
            _origin(path, _ENTRYPOINT),
            stat.st_size,
            stat.st_mtime,
            companion if companion.is_dir() else None,
        )


def codex_transcripts(root: Path) -> Iterable[Transcript]:
    """``<root>/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl`` (root is ``~/.codex/sessions``)."""
    if not root.is_dir():
        return
    for path in root.rglob("rollout-*.jsonl"):
        stat = path.stat()
        ids = _UUID.findall(path.stem)
        yield Transcript(
            "codex", path, ids[-1] if ids else path.stem, _origin(path, _ORIGINATOR), stat.st_size, stat.st_mtime
        )


def candidates(
    transcripts: Iterable[Transcript],
    *,
    older_than_days: float,
    protected_ids: set[str],
    now: float | None = None,
) -> tuple[list[Transcript], dict[str, int]]:
    """Automated, old enough and not aliased; plus a count of why the rest were kept."""
    if not math.isfinite(older_than_days) or older_than_days < 0:
        raise ValueError("older_than_days must be a finite number >= 0")
    cutoff = (time.time() if now is None else now) - older_than_days * 86400
    chosen: list[Transcript] = []
    kept = {"not automated": 0, "unknown origin": 0, "too recent": 0, "aliased": 0}
    for item in transcripts:
        if item.origin is None:
            kept["unknown origin"] += 1
        elif not item.automated:
            kept["not automated"] += 1
        elif item.mtime > cutoff:
            kept["too recent"] += 1
        elif item.session_id in protected_ids:
            kept["aliased"] += 1
        else:
            chosen.append(item)
    return chosen, kept


def delete(items: Iterable[Transcript]) -> tuple[int, int, list[str]]:
    """Delete transcripts (and Claude sub-agent folders); returns (count, bytes, errors)."""
    count = freed = 0
    errors: list[str] = []
    for item in items:
        size = item.total_size
        try:
            item.path.unlink()
            if item.companion is not None and item.companion.is_dir():
                shutil.rmtree(item.companion)
        except OSError as exc:
            errors.append(f"{item.path}: {exc}")
            continue
        count += 1
        freed += size
    return count, freed, errors


def default_roots() -> dict[str, Path]:
    home = Path(os.path.expanduser("~"))
    return {
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "projects",
        "codex": Path(os.environ.get("CODEX_HOME", home / ".codex")) / "sessions",
    }
