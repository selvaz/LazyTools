"""Instructions an agent loads as it needs them, from one Markdown file.

An agent with a large specialised job does not need all of its instructions at
once. It needs an identity, a catalogue of what else exists, and the ability to
pull in the part that applies. Loading everything makes the prompt enormous and
buries the two paragraphs that mattered; loading nothing makes the agent
generic.

The whole library lives in ONE file per agent, split into named blocks. That is
a deliberate choice over a tree of files: it removes ids derived from filenames
(which behave differently on Windows and Linux), path containment, and the class
of bug where a file is edited and nothing notices. One file is also one diff.

Three things are enforced rather than requested, because an instruction that
merely asks is not a constraint:

* **Loading is by id**, from a catalogue. A path from a caller is never opened.
* **Dependencies are resolved transitively and deduplicated**, so a block that
  needs another cannot be read without it.
* **A budget in characters is applied**, and a request that would exceed it is
  refused whole rather than truncated — half an instruction is worse than none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: A block opens with "## block: <id>" and its header runs to a "---" line.
_BLOCK = re.compile(r"^##\s*block:\s*(?P<id>[\w/.-]+)\s*$", re.M)
_HEADER_END = re.compile(r"^---\s*$", re.M)
#: How much instruction text one request may return. Generous enough for a
#: dependency closure of half a dozen blocks, small enough that loading the
#: whole library is impossible by accident.
DEFAULT_BUDGET = 40_000


class BlockError(ValueError):
    """The library is malformed, or a request cannot be satisfied."""


@dataclass(frozen=True)
class Block:
    """One instruction module."""

    id: str
    kind: str
    summary: str
    requires: tuple[str, ...]
    body: str

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass(frozen=True)
class Library:
    """Every block of one agent, validated on construction."""

    blocks: dict[str, Block]
    source: str

    def catalogue(self) -> str:
        """One line per block: what exists, and when it applies.

        This is the only part of the library that belongs in a static system
        prompt. It is what makes the rest reachable, and it is small.
        """
        return "\n".join(f"- {b.id} ({b.kind}): {b.summary}" for b in self.blocks.values())

    def resolve(self, ids: list[str] | tuple[str, ...]) -> list[Block]:
        """The requested blocks and everything they require, in dependency order.

        Raises:
            BlockError: for an unknown id. Silently dropping one would return a
                closure that is missing a piece nobody was told about.
        """
        unknown = [i for i in ids if i not in self.blocks]
        if unknown:
            raise BlockError(
                f"no such block: {', '.join(unknown)}. Load by id from the catalogue; "
                f"available: {', '.join(sorted(self.blocks))}"
            )
        ordered: list[Block] = []
        seen: set[str] = set()

        def visit(block_id: str, path: tuple[str, ...]) -> None:
            if block_id in seen:
                return
            if block_id in path:
                raise BlockError(f"blocks require each other in a cycle: "
                                 f"{' -> '.join((*path, block_id))}")
            for dependency in self.blocks[block_id].requires:
                visit(dependency, (*path, block_id))
            seen.add(block_id)
            ordered.append(self.blocks[block_id])

        for block_id in ids:
            visit(block_id, ())
        return ordered

    def render(self, ids: list[str] | tuple[str, ...], *, budget: int = DEFAULT_BUDGET) -> str:
        """The resolved blocks as text, or a refusal.

        Raises:
            BlockError: when the closure exceeds ``budget``. Refused whole: a
                truncated instruction reads as a complete one.
        """
        blocks = self.resolve(ids)
        total = sum(b.size for b in blocks)
        if total > budget:
            raise BlockError(
                f"loading {', '.join(b.id for b in blocks)} needs {total:,} characters "
                f"against a budget of {budget:,}. Load fewer blocks rather than part of "
                "these: half an instruction reads as a whole one."
            )
        return "\n\n".join(f"## {b.id}\n\n{b.body}" for b in blocks)


def load_library(path: str | Path) -> Library:
    """Parse and validate one agent's instruction file.

    Raises:
        BlockError: on a duplicate id, a requirement that does not exist, a
            block with no summary, or a cycle. All of it at load time, because a
            library that is wrong should fail before an agent runs, not during.
    """
    text = Path(path).read_text(encoding="utf-8")
    matches = list(_BLOCK.finditer(text))
    if not matches:
        raise BlockError(f"{path} contains no blocks; expected '## block: <id>' headings")

    blocks: dict[str, Block] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = _parse_block(match.group("id"), text[start:end], path)
        if block.id in blocks:
            raise BlockError(f"{path}: block {block.id!r} is defined twice")
        blocks[block.id] = block

    for block in blocks.values():
        missing = [r for r in block.requires if r not in blocks]
        if missing:
            raise BlockError(f"{path}: block {block.id!r} requires "
                             f"{', '.join(missing)}, which do not exist")

    library = Library(blocks=blocks, source=str(path))
    for block_id in blocks:
        library.resolve([block_id])  # raises on a cycle
    return library


def _parse_block(block_id: str, chunk: str, path: str | Path) -> Block:
    header_end = _HEADER_END.search(chunk)
    if header_end is None:
        raise BlockError(f"{path}: block {block_id!r} has no '---' separating its header")
    header, body = chunk[: header_end.start()], chunk[header_end.end():].strip()
    fields = {
        key.strip().lower(): value.strip()
        for key, _, value in (line.partition(":") for line in header.splitlines())
        if key.strip()
    }
    summary = fields.get("summary", "")
    if not summary:
        raise BlockError(f"{path}: block {block_id!r} has no summary; the catalogue is "
                         "how an agent knows the block exists")
    if not body:
        raise BlockError(f"{path}: block {block_id!r} has no body")
    requires = tuple(r.strip() for r in fields.get("requires", "").split(",") if r.strip())
    return Block(id=block_id, kind=fields.get("kind", "process"), summary=summary,
                 requires=requires, body=body)


def instruction_tools(library: Library, *, budget: int = DEFAULT_BUDGET) -> list:
    """One batch tool that loads instruction blocks by id.

    One tool, not one per block: a tool's schema is sent on every turn, so a
    tool per block would put the whole catalogue in every request and defeat the
    point of loading anything lazily.
    """
    from lazybridge import Tool

    def load_instructions(block_ids: str) -> str:
        """Load instruction blocks by id, with everything they require.

        Args:
            block_ids: comma-separated ids from the catalogue in your system
                prompt, e.g. "process/leverage, process/liquidity".

        Returns the blocks' text. Ids that do not exist, or a request too large
        to return whole, come back as an error naming what to do instead.
        """
        ids = [i.strip() for i in block_ids.split(",") if i.strip()]
        if not ids:
            return "load_instructions needs at least one block id from the catalogue."
        try:
            return library.render(ids, budget=budget)
        except BlockError as exc:
            return str(exc)

    return [Tool.wrap(load_instructions, name="load_instructions")]


__all__ = [
    "DEFAULT_BUDGET",
    "Block",
    "BlockError",
    "Library",
    "instruction_tools",
    "load_library",
]
