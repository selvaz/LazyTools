"""The bundled ``agent-operations`` skill: structure, real commands, no private data.

Every ``lazytools-code-bridge`` / ``lazytools-projects`` command written in the
skill is parsed against the real argparse definitions, so a renamed flag or
subcommand fails here instead of misleading an assistant.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

from lazytools.code_bridge import cli as bridge_cli
from lazytools.projects import cli as projects_cli
from lazytools.skills import build_packaged_skill, packaged_skill_dir, packaged_skills, query_skill
from lazytools.skills.agent_operations import SKILL_NAME, reference_paths, skill_path
from lazytools.skills.agent_operations import __main__ as installer
from lazytools.skills.doc_skills import _frontmatter_description

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = skill_path()
SKILL_FILES = [SKILL_DIR / "SKILL.md", *reference_paths()]
PROGRAMS = {
    "lazytools-code-bridge": bridge_cli.build_parser,
    "lazytools-projects": projects_cli.build_parser,
}


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    head = text.split("\n---\n", 1)[0]
    return {m[1]: m[2].strip() for m in re.finditer(r"^(\w+):\s*(.*)$", head, re.M)}


def test_frontmatter_names_the_skill_and_describes_it():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    meta = _frontmatter(text)
    assert meta["name"] == SKILL_NAME == "agent-operations"
    description = _frontmatter_description(text)
    assert 200 < len(description) < 1024
    for term in ("lazytools-code-bridge", "--review-of", "close-task", "--where cloud"):
        assert term in description


def test_every_reference_is_routed_from_the_index_and_exists():
    index = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"references/([\w-]+\.md)", index))
    on_disk = {path.name for path in reference_paths()}
    assert linked == on_disk
    assert {"code-bridge.md", "routing-and-review.md", "projects.md", "briefs.md",
            "merge-and-release.md", "working-lessons.md"} <= on_disk


def test_working_lessons_has_twenty_numbered_lessons_each_with_a_reason():
    text = (SKILL_DIR / "references" / "working-lessons.md").read_text(encoding="utf-8")
    numbers = [int(n) for n in re.findall(r"^(\d+)\. \*\*", text, re.M)]
    assert numbers == list(range(1, 21))
    assert text.count("Why:") == 20


# --------------------------------------------------------------------------- #
# Commands written in the skill exist
# --------------------------------------------------------------------------- #


def _command_lines(text: str) -> list[str]:
    """Commands from fenced blocks (continuations joined) and inline code spans."""
    lines: list[str] = []
    for block in re.findall(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S):
        lines.extend(block.replace("\\\n", " ").splitlines())
    lines.extend(re.findall(r"`((?:lazytools-code-bridge|lazytools-projects) [^`]+)`", text))
    commands: list[str] = []
    for line in lines:
        line = re.split(r"\s#\s", line, maxsplit=1)[0].strip()
        program = None
        for segment in (part.strip() for part in line.split(" | ")):
            first = segment.split(" ", 1)[0]
            if first in PROGRAMS:
                program = first
                commands.append(segment)
            elif program is not None:  # "approve X | reject X --reason ..." shorthand
                commands.append(f"{program} {segment}")
    return commands


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("parser has no subcommands")


def _check_command(command: str) -> list[str]:
    tokens = shlex.split(command)
    subs = _subparsers(PROGRAMS[tokens[0]]())
    if len(tokens) < 2 or tokens[1] not in subs:
        return [f"{command!r}: unknown subcommand"]
    options = set(subs[tokens[1]]._option_string_actions)
    return [f"{command!r}: unknown option {tok}" for tok in tokens[2:]
            if tok.startswith("--") and tok.split("=", 1)[0] not in options]


def test_every_cli_command_in_the_skill_parses_against_the_real_cli():
    commands = [cmd for path in SKILL_FILES for cmd in _command_lines(path.read_text(encoding="utf-8"))]
    assert len(commands) > 30, "the extractor stopped finding commands"
    subs_used = {shlex.split(cmd)[1] for cmd in commands if cmd.startswith("lazytools-code-bridge")}
    assert {"run", "route", "wait", "pending", "approve", "reject", "result", "jobs",
            "cancel", "apply"} <= subs_used
    problems = [problem for cmd in commands for problem in _check_command(cmd)]
    assert problems == []


def test_documented_tier_and_cloud_choices_match_the_cli():
    run = _subparsers(bridge_cli.build_parser())["run"]
    actions = {a.dest: a for a in run._actions}
    assert set(actions["tier"].choices) == {"basic", "writing", "thinking", "critical"}
    assert set(actions["where"].choices) == {"local", "cloud"}
    text = (SKILL_DIR / "references" / "code-bridge.md").read_text(encoding="utf-8")
    documented = re.search(r"--approve-cloud\s+([a-z|-]+)", text)[1].split("|")
    assert set(documented) == set(actions["approve_cloud"].choices)


def test_projects_python_snippets_run_against_a_real_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = (SKILL_DIR / "references" / "projects.md").read_text(encoding="utf-8")
    namespace: dict = {}
    for code in re.findall(r"^```python\n(.*?)^```", text, re.M | re.S):
        exec(compile(code, "<agent-operations projects.md>", "exec"), namespace)
    assert "Implement retry" in namespace["gantt"].render_gantt_text(
        namespace["store"], statuses=None, now=namespace["now"])
    contract = namespace["contracts"].find_contract_for_task(namespace["store"], "svc-retry", 0)
    assert contract is not None


# --------------------------------------------------------------------------- #
# Public and generic: nothing tied to one user, machine or domain
# --------------------------------------------------------------------------- #

_FORBIDDEN = [
    (r"[A-Za-z]:\\\\?(?:Users|ProgramData)", "Windows user path"),
    (r"/home/|/Users/|/root/", "home directory path"),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "email address"),
    (r"(?i)selvaz", "owner name"),
    (r"(?i)\b(?:financ\w*|invest\w*|trading|portfolio|lazyceo)\b", "domain-specific term"),
    (r"(?i)\b(?:sk-[a-z0-9]{8,}|ghp_[a-z0-9]{8,}|github_pat_)", "credential-looking token"),
    (r"https?://(?!github\.com/owner/repo)", "external URL"),
]


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_skill_contains_no_private_or_domain_specific_data(path):
    text = path.read_text(encoding="utf-8")
    found = [f"{label}: {m.group(0)!r}" for pattern, label in _FORBIDDEN for m in re.finditer(pattern, text)]
    # ~/ paths are allowed only for the tools' own documented config locations.
    found += [f"home path: {m!r}" for m in re.findall(r"~/[\w./-]+", text)
              if not m.startswith(("~/.lazytools/", "~/.claude", "~/.codex"))]
    assert found == []


# --------------------------------------------------------------------------- #
# Registration and installation
# --------------------------------------------------------------------------- #


def test_registered_as_a_packaged_doc_skill_and_queryable(tmp_path):
    assert "agent-operations" in packaged_skills()
    assert packaged_skill_dir("agent-operations") == SKILL_DIR
    with pytest.raises(KeyError, match="available: agent-operations"):
        packaged_skill_dir("nope")
    meta = build_packaged_skill("agent-operations", output_root=str(tmp_path))
    assert Path(meta["skill_dir"]).name == "agent-operations"
    assert len(meta["indexed_files"]) == len(SKILL_FILES)
    assert meta["description"].startswith("Use when an LLM assistant")
    brief = query_skill(meta["skill_dir"], "how do I get an opposite engine review with --review-of", mode="locate")
    assert "routing-and-review.md" in brief


def test_install_copies_the_folder_without_python_files(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "skills" / "agent-operations"
    assert installer.main(["install", "--dest", str(dest)]) == 0
    assert (dest / "SKILL.md").is_file()
    assert sorted(p.name for p in (dest / "references").glob("*.md")) == [p.name for p in reference_paths()]
    assert not list(dest.rglob("*.py"))
    # A reinstall replaces its own folder; a foreign folder is refused.
    assert installer.main(["install", "--dest", str(dest)]) == 0
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    assert installer.main(["install", "--dest", str(foreign)]) == 1
    assert "refused" in capsys.readouterr().err

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home" / ".codex").mkdir(parents=True)
    assert installer.main(["install"]) == 0
    assert (tmp_path / "home" / ".codex" / "skills" / "agent-operations" / "SKILL.md").is_file()
    assert not (tmp_path / "home" / ".claude").exists()


def test_linked_from_readme_and_docs():
    target = "src/lazytools/skills/agent_operations/SKILL.md"
    assert target in (ROOT / "README.md").read_text(encoding="utf-8")
    assert target in (ROOT / "docs" / "skills.md").read_text(encoding="utf-8")
