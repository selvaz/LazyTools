# Code bridge cloud setup skill

LazyTools ships one **agent skill**: a `SKILL.md` instruction file that an
LLM assistant follows to walk you through setting up
[cloud jobs for the code bridge](code-bridge-cloud-setup.md) (Codex Cloud
and/or Claude Code cloud sessions).

Unlike the [documentation skills](skills.md) (BM25 bundles built from your
docs), agent skills are hand-written guides in the common `SKILL.md` format:
YAML front matter with `name` and `description`, then Markdown instructions.
They live in the package under `src/lazytools/skills/agent_skills/<name>/SKILL.md`.

## What the skill does

The assistant:

1. **checks every prerequisite with a command** instead of asking:
   `git remote get-url origin`, `git status -sb`, `codex --version`,
   `claude --version`, `claude auth status` (where the CLI has it),
   `gh auth status`, whether `ANTHROPIC_API_KEY` is set (cloud sessions need
   the claude.ai login, not an API key), and where `code-bridge.toml` is;
2. **asks only for what exists only in a web UI**: which engines you want, the
   Codex Cloud environment id, confirmation that the Claude default
   environment and the GitHub App are set up, and optional routine ids;
3. **fills `code-bridge.toml`** minimally (`[cloud]`, `[cloud.codex.envs]`,
   `[cloud.claude.routines]`), keeping every existing table, and validates it
   with the bridge's own loader;
4. proposes a harmless first `run --where cloud` and explains what `status`,
   `result` and `apply` will show for each engine.

It never asks you to paste a token into the chat. Routine tokens go in an
environment variable you set, and the config stores only the variable's name.
It never pushes or merges for you.

## Install it

```bash
lazytools-code-bridge setup-skill                 # print the path of the bundled SKILL.md
lazytools-code-bridge setup-skill --install       # copy to ~/.claude/skills/code-bridge-cloud-setup/SKILL.md
lazytools-code-bridge setup-skill --install .claude/skills   # or into a project
```

Then ask Claude Code something like *"set up lazytools-code-bridge cloud
jobs for this repo"*. It picks the skill up by its description. For other
assistants, give them the file path.

From Python:

```python
from lazytools.skills.agent_skills import agent_skill_path, install_agent_skill, list_agent_skills

list_agent_skills()                       # ['code-bridge-cloud-setup']
agent_skill_path("code-bridge-cloud-setup")
install_agent_skill("code-bridge-cloud-setup", overwrite=True)
```
