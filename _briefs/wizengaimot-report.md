# WizengAImot implementation report

## Changes by file

- `src/lazytools/skills/council.py`: motion proposal and current-motion voting;
  quorum and closing use only current-motion alignment; synthesis receives the
  current and winning motion; results expose the winning text.
- `tests/test_council.py`: offline fake-driven coverage of no motion, opposing
  motions, stale votes, quorum and refused closure, plus result and synthesis.
- `src/lazytools/skill/wizengaimot/SKILL.md`: public interactive skill with
  setup, privacy, failure recovery, blind first opinions, alternating
  cross-examination, and a verbale.
- `src/lazytools/skill/__main__.py`: installs both skills for selected homes or
  installs WizengAImot beside the `--dest` LazyTools directory.
- `pyproject.toml`: documents inclusion of both packaged skill directories.
- `tests/test_public_skill.py`: verifies installation, individual assistant
  targets, BOM-free frontmatter, wheel contents, and consultant tool names.
- `docs/skills-council.md`: one guide for the skill and Python council.
- `CHANGELOG.md`: unreleased entries for both changes.
- `_briefs/wizengaimot-report.md`: this report.

## Council API

The discussion exposes these tools to agents, rather than as methods to call on
`WizengAImot` from application code:

- Members and moderator: `propose_motion(text: str) -> str`, returning the new
  `motion:N` ID and text.
- Members: `cast_vote(motion_id: str, aligned: bool, confidence: float,
  rationale: str) -> str`. Confidence is between 0 and 1; rationale is required.
- Members and moderator: `route(agent_name: str, task: str) -> str`.
- Moderator: `check_quorum() -> str`, `close_discussion(summary: str) -> str`.
  Closing concludes the discussion only with quorum.

`CouncilResult` fields are `question: str`, `quorum_reached: bool`,
`votes: list[dict]`, `transcript: list[dict]`, `synthesis: str`, and
`motion: str | None`. Votes contain `member`, `motion_id`, `aligned`,
`confidence`, and `rationale`; the most recent vote per member is retained,
including stale votes for transparency. `text()` returns the synthesis.
`motion` is populated only when quorum is reached.

## Verification

- `python -m pytest tests/test_council.py tests/test_public_skill.py -q`: 22 passed.
- `python -m pytest tests/test_council.py::test_motion_tools_enforce_quorum_and_report_winner -q`:
  1 passed after the status display clarification.
- `python -m ruff check src/lazytools/skills/council.py src/lazytools/skill/__main__.py tests/test_council.py tests/test_public_skill.py`: passed; the council file was rechecked after the final clarification.
- `python -m mkdocs build --strict`: passed. MkDocs noted an existing page outside nav and displayed the Material team's MkDocs 2.0 advisory.

Commands used `C:\ProgramData\spyder-6\python.exe` and `PYTHONPATH=src` for
Python tests. No network calls were made by the tests. The full suite was not run.

## Open items

None. `_incoming/` and the pre-existing brief remain untracked and were not
included in commits.
