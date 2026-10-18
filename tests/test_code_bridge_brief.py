"""The pre-launch brief check: the pure rules, and their wiring into ``run``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _brief, _jobs, _store, cli
from lazytools.code_bridge._brief import check_brief
from test_code_bridge_routing import bridge as _routing_bridge  # noqa: F401  (registers the shared `bridge` fixture)
from test_code_bridge_routing import seed

REPO = r"C:\Users\Administrator\Documents\GitHub\LazyTools"

GOOD = """\
# Brief: add the thing

## Goal
Make `run` print the thing before it launches, so a caller sees it without reading a log.

## Context
- Package `src/lazytools/code_bridge/`; the change belongs in `cli.py`.

## Constraints
- Do not touch the approval rules.

## Done when
- `pytest -q tests/test_thing.py` passes.
- ruff is clean on the touched files.

## Report
The diff summary and the test output.
"""


def codes(findings):
    return [f.code for f in findings]


def check(text, *, engine="claude", cwd=REPO, root=None):
    return check_brief(text, engine=engine, cwd=cwd, root=root)


def without(text, heading):
    """GOOD with one section's heading line removed."""
    return text.replace(f"## {heading}\n", "")


# ----------------------------------------------------------------- sections


def test_complete_brief_has_no_findings():
    assert len(GOOD) > 200
    assert check(GOOD) == []


@pytest.mark.parametrize(
    "heading,code,severity",
    [
        ("Goal", "missing-goal", "error"),
        ("Done when", "missing-done-when", "error"),
        ("Context", "missing-context", "warn"),
        ("Constraints", "missing-constraints", "warn"),
        ("Report", "missing-report", "warn"),
    ],
)
def test_each_missing_section_is_reported_with_its_severity(heading, code, severity):
    findings = check(without(GOOD, heading))
    assert [(f.code, f.severity) for f in findings] == [(code, severity)]
    assert heading in findings[0].message


@pytest.mark.parametrize(
    "done_when",
    [
        "## Done when",
        "## Done when:",
        "## DONE WHEN",
        "### done when",
        "#Done when",
        "**Done when**",
        "Done when:",
        "## Done  when",
    ],
)
def test_heading_match_is_case_insensitive_and_tolerates_a_colon(done_when):
    assert "missing-done-when" not in codes(check(GOOD.replace("## Done when", done_when)))


@pytest.mark.parametrize(
    "english,italian",
    [
        ("Goal", "Obiettivo"),
        ("Context", "Contesto"),
        ("Constraints", "Vincoli"),
        ("Done when", "Fatto quando"),
        ("Report", "Rapporto"),
    ],
)
def test_italian_headings_are_accepted(english, italian):
    assert check(GOOD.replace(f"## {english}", f"## {italian}:")) == []


def test_fully_italian_brief_is_complete():
    brief = GOOD
    for english, italian in [
        ("Goal", "Obiettivo"),
        ("Context", "Contesto"),
        ("Constraints", "Vincoli"),
        ("Done when", "Fatto quando"),
        ("Report", "Rapporto"),
    ]:
        brief = brief.replace(f"## {english}", f"## {italian}")
    assert check(brief) == []


def test_section_word_inside_a_sentence_is_not_a_heading():
    findings = check(without(GOOD, "Goal") + "\nThe goal is to be quick. Report back when done when ready.\n")
    assert "missing-goal" in codes(findings)
    assert "missing-report" not in codes(findings)  # the real '## Report' is still there


def test_template_quoted_in_a_code_fence_does_not_count():
    fenced = "Use this template:\n\n```text\n## Goal\n## Done when\n```\n" + "x" * 200
    assert {"missing-goal", "missing-done-when"} <= set(codes(check(fenced)))


def test_empty_brief_misses_every_section():
    assert {f.code for f in check("")} >= {
        "missing-goal",
        "missing-done-when",
        "missing-context",
        "missing-constraints",
        "missing-report",
    }


# -------------------------------------------------------------------- paths


def test_claude_path_outside_cwd_is_an_error_naming_the_path():
    outside = r"C:\Users\Administrator\Documents\GitHub\_design\spec.md"
    findings = check(GOOD + f"\nRead {outside} first.\n")
    assert [(f.code, f.severity) for f in findings] == [("path-outside-cwd", "error")]
    assert outside in findings[0].message


def test_claude_path_inside_cwd_is_fine():
    assert check(GOOD + rf"\nEdit {REPO}\src\lazytools\code_bridge\cli.py and {REPO}/docs/code-bridge.md.") == []


def test_cwd_itself_and_forward_slashes_and_case_are_inside():
    text = (
        GOOD
        + "\nSee c:/users/administrator/documents/github/lazytools and C:\\Users\\Administrator\\Documents\\GitHub\\LazyTools\\x.\n"
    )
    assert check(text) == []


def test_sibling_directory_sharing_a_name_prefix_is_outside():
    sibling = REPO + "-mcp-main"
    findings = check(GOOD + f"\nDo not modify {sibling}.\n")
    assert codes(findings) == ["path-outside-cwd"]
    assert sibling in findings[0].message


def test_dotdot_escaping_cwd_is_outside():
    assert codes(check(GOOD + rf"\nRead {REPO}\..\other\x.md")) == ["path-outside-cwd"]


def test_codex_may_name_paths_anywhere_under_the_root():
    text = GOOD + r"\nRead C:\Users\Administrator\Documents\GitHub\_design\spec.md and /srv/shared/notes.md."
    assert check(text, engine="codex") == []
    assert codes(check(text, engine="claude")) == ["path-outside-cwd", "path-outside-cwd"]


def test_posix_path_outside_cwd_is_an_error():
    findings = check(GOOD + "\nRead /srv/shared/notes.md before starting.\n", cwd="/home/me/repo")
    assert codes(findings) == ["path-outside-cwd"]
    assert "/srv/shared/notes.md" in findings[0].message
    assert check(GOOD + "\nEdit /home/me/repo/src/a.py.\n", cwd="/home/me/repo") == []


def test_git_bash_drive_form_is_compared_with_the_windows_cwd():
    assert check(GOOD + "\nEdit /c/Users/Administrator/Documents/GitHub/LazyTools/a.py\n") == []
    assert codes(check(GOOD + "\nRead /c/Users/Administrator/other/a.py\n")) == ["path-outside-cwd"]


def test_interpreter_dir_urls_placeholders_and_plain_slashes_are_ignored():
    text = GOOD + (
        "\nRun C:\\ProgramData\\spyder-6\\python.exe -m pytest -q."
        "\nSee https://github.com/selvaz/LazyTools/pull/204 and file:///C:/Other/x."
        "\nA review of C:\\...\\GitHub\\_design\\... stalled; use C:\\Users\\<name>\\x."
        "\nPick pass/fail, and/or origin/main, 17/10, `error` / `warn`, via /help."
    )
    assert check(text) == []


def test_trailing_punctuation_is_not_part_of_the_path():
    findings = check(GOOD + "\nRead C:\\Other\\notes.md.\n")
    assert "C:\\Other\\notes.md is outside" in findings[0].message


def test_each_distinct_path_is_reported_once_and_the_disk_is_not_consulted(tmp_path):
    ghost = r"D:\does\not\exist.md"
    assert not Path(ghost).exists()
    findings = check(GOOD + f"\n{ghost} then {ghost} again, and {REPO}\\no\\such\\file.py\n")
    assert codes(findings) == ["path-outside-cwd"]


def test_cwd_may_be_a_path_object(tmp_path):
    inside = tmp_path / "repo" / "a.py"
    assert check(GOOD + f"\nEdit {inside}\n", cwd=tmp_path / "repo") == []
    assert codes(check(GOOD + f"\nEdit {tmp_path / 'other' / 'a.py'}\n", cwd=tmp_path / "repo")) == ["path-outside-cwd"]


# ------------------------------------------------------ paths containing spaces

POSIX_CWD = "/home/me/my repo"
WIN_CWD = r"C:\Users\Me Name\my repo"


@pytest.mark.parametrize(
    "line",
    [
        "Edit /home/me/my repo/src/a.py first.",
        "Edit /home/me/my repo/src/sub dir/a b.py first.",
        'Edit "/home/me/my repo/src/a.py" first.',
        "Edit `/home/me/my repo/src/a.py` first.",
        "Work in /home/me/my repo.",
        "Work in /home/me/my repo and src/a.py.",
        "(/home/me/my repo)",
    ],
)
def test_posix_cwd_with_a_space_is_not_cut_at_the_space(line):
    assert check(GOOD + f"\n{line}\n", cwd=POSIX_CWD) == []


@pytest.mark.parametrize(
    "line",
    [
        r"Edit C:\Users\Me Name\my repo\src\a.py first.",
        r"Edit c:/users/me name/my repo/src/a.py first.",
        r'Edit "C:\Users\Me Name\my repo\src\a b.py" first.',
        r"Edit `C:\Users\Me Name\my repo` first.",
        r"Work in C:\Users\Me Name\my repo.",
    ],
)
def test_windows_cwd_with_a_space_is_not_cut_at_the_space(line):
    assert check(GOOD + f"\n{line}\n", cwd=WIN_CWD) == []


@pytest.mark.parametrize(
    "line,named",
    [
        ("Read /home/me/my repo copy/file.py now.", "/home/me/my repo copy/file.py"),
        ('Read "/home/me/my repo copy/file.py" now.', "/home/me/my repo copy/file.py"),
        ('Read "/home/me/my repo old/two words/file.py" now.', "/home/me/my repo old/two words/file.py"),
        ("Read `/home/me/my repo-old/file.py` now.", "/home/me/my repo-old/file.py"),
        ("Read /home/me/my repo.bak/file.py now.", "/home/me/my repo.bak/file.py"),
        ("Read /home/me/other dir/file.py now.", "/home/me/other dir/file.py"),
    ],
)
def test_outside_path_with_spaces_is_flagged_and_named_whole(line, named):
    findings = check(GOOD + f"\n{line}\n", cwd=POSIX_CWD)
    assert codes(findings) == ["path-outside-cwd"]
    assert f"{named} is outside" in findings[0].message


@pytest.mark.parametrize(
    "line,named",
    [
        (r"Read C:\Users\Me Name\my repo copy\a.py now.", r"C:\Users\Me Name\my repo copy\a.py"),
        (r'Read "C:\Users\Me Name\my repo copy\two words\a.py" now.', r"C:\Users\Me Name\my repo copy\two words\a.py"),
        (r"Read C:\Program Files\Tool\a.exe now.", r"C:\Program Files\Tool\a.exe"),
        (r"Read `C:\Program Files (x86)\Tool\a.exe`.", r"C:\Program Files (x86)\Tool\a.exe"),
    ],
)
def test_windows_outside_path_with_spaces_is_flagged_and_named_whole(line, named):
    findings = check(GOOD + f"\n{line}\n", cwd=WIN_CWD)
    assert codes(findings) == ["path-outside-cwd"]
    assert f"{named} is outside" in findings[0].message


def test_prose_after_a_path_with_spaces_is_not_absorbed_into_it():
    text = GOOD + "\nRead /srv/shared/notes.md and the other file, then edit /home/me/my repo/a.py.\n"
    findings = check(text, cwd=POSIX_CWD)
    assert [f.message.split(" is outside")[0] for f in findings] == ["/srv/shared/notes.md"]


def test_quoted_span_is_one_path_even_with_a_command_after_it():
    text = GOOD + "\nRun `C:\\ProgramData\\spyder-6\\python.exe -m pytest -q` and `/home/me/my repo/run it.sh`.\n"
    assert check(text, cwd=POSIX_CWD) == []


def test_known_limit_unquoted_sibling_with_a_multi_word_name_is_not_caught():
    # Documented trade-off: only a quoted path, or a sibling whose first extra word
    # is followed by a separator, is told apart from prose after the cwd.
    assert check(GOOD + "\nRead /home/me/my repo two words/file.py\n", cwd=POSIX_CWD) == []
    assert codes(check(GOOD + '\nRead "/home/me/my repo two words/file.py"\n', cwd=POSIX_CWD)) == ["path-outside-cwd"]


# ------------------------------------------------------------------ quoting


@pytest.mark.parametrize(
    "line,code",
    [
        ("Run `gh pr list --json number --jq '.[0].number'` for the id.", "quoting-jq"),
        ('Run `gh pr view 3 --json title --jq ".title"`.', "quoting-jq"),
        ("gh pr list --json number --jq '.[0]'", "quoting-jq"),
        ("- $ gh run list --jq '.[]'", "quoting-jq"),
        ('Commit with `git commit -m "fix the thing"`.', "quoting-commit-message"),
        ('git commit -am "wip"'.replace("-am", "-a -m"), "quoting-commit-message"),
        ("1. git commit -m 'msg'", "quoting-commit-message"),
    ],
)
def test_quoted_arguments_in_a_command_are_warned(line, code):
    findings = check(GOOD + f"\n{line}\n")
    assert [(f.code, f.severity) for f in findings] == [(code, "warn")]


def test_command_inside_a_fenced_block_is_checked():
    text = GOOD + "\n```sh\ncd repo\nrun-it --jq '.x'\ngh api x --jq '.y'\n```\n"
    assert codes(check(text)) == ["quoting-jq"]


def test_fenced_block_with_no_quoting_is_fine():
    assert check(GOOD + "\n```sh\ngh pr list --json number\ngit commit -F .tmp/msg.txt\n```\n") == []


@pytest.mark.parametrize(
    "line",
    [
        "Use `gh ... --json` without `--jq '<filter>'`, and `git commit -F <file>` instead of `-m \"<text>\"`.",
        "Use `gh pr list --json number` and filter the output yourself.",
        "Write the message to a file, then `git commit -F .tmp/msg.txt`.",
        "The jq filter '.x' and the -m \"flag\" are fine in prose.",
        "Run `python -m \"pkg\"` and `echo --jq 'x'`.",
    ],
)
def test_quoting_in_prose_or_a_bare_flag_or_a_safe_command_is_not_flagged(line):
    assert check(GOOD + f"\n{line}\n") == []


def test_each_quoting_pattern_is_reported_once():
    text = GOOD + "\ngh a --jq '.x'\ngh b --jq '.y'\ngit commit -m \"a\"\ngit commit -m \"b\"\n"
    assert codes(check(text)) == ["quoting-jq", "quoting-commit-message"]


# -------------------------------------------------------------------- style


@pytest.mark.parametrize("line", ["You are a senior engineer.", "you are an expert.", "  YOU ARE A careful reviewer."])
def test_persona_line_is_a_warning(line):
    findings = check(f"{line}\n\n{GOOD}")
    assert [(f.code, f.severity) for f in findings] == [("persona-line", "warn")]


def test_you_are_inside_a_sentence_or_with_another_article_is_not_a_persona():
    assert check(GOOD + "\nIf you are a maintainer, merge. You are the owner of this repo.\n") == []


@pytest.mark.parametrize("phrase", ["Think step by step.", "think step-by-step", "Please THINK  STEP BY STEP first"])
def test_think_step_by_step_is_a_warning(phrase):
    findings = check(GOOD + f"\n{phrase}\n")
    assert [(f.code, f.severity) for f in findings] == [("step-by-step", "warn")]


def test_thinking_about_steps_is_not_flagged():
    assert check(GOOD + "\nWork in small steps; think about each one.\n") == []


# ------------------------------------------------------------------- length


def test_short_brief_gets_exactly_one_length_warning():
    findings = [f for f in check("Fix the bug.") if f.code == "brief-too-short"]
    assert [(f.severity, f.message) for f in findings] == [("warn", "brief too short to carry context")]


def test_length_boundary_ignores_surrounding_whitespace():
    assert "brief-too-short" in codes(check("x" * 199))
    assert "brief-too-short" not in codes(check("x" * 200))
    assert "brief-too-short" in codes(check("   " + "x" * 199 + "\n\n"))


def test_finding_format_is_one_line():
    line = check("Fix the bug.")[0].format()
    assert "\n" not in line and line.split(" ")[0] in {"error", "warn"}


# --------------------------------------------------------------- wiring: run


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("LAZYTOOLS_CODE_BRIDGE_POLL_SECONDS", "0.05")
    path = tmp_path / "repo"
    path.mkdir()
    return path


def run_args(tmp_path, repo, task, *extra, engine="codex"):
    return [
        "run",
        "--engine",
        engine,
        "--cwd",
        str(repo),
        "--root",
        str(tmp_path),
        "--db",
        str(tmp_path / "db.sqlite"),
        "--task",
        task,
        *extra,
    ]


def job_count(tmp_path):
    return len(_jobs.list_jobs(_store.build_store(tmp_path / "db.sqlite"), all_jobs=True))


def brief_lines(err):
    return [line for line in err.splitlines() if line.startswith("brief-check:")]


def test_default_run_prints_findings_and_still_launches(tmp_path, repo, monkeypatch, capsys):
    script = fakes.install(monkeypatch)
    assert cli.main(run_args(tmp_path, repo, "do it")) == 0
    captured = capsys.readouterr()
    lines = brief_lines(captured.err)
    assert any("error missing-goal" in line for line in lines)
    assert any("warn brief-too-short" in line for line in lines)
    assert all("\n" not in line for line in lines)
    assert script.prompts == ["do it"]
    assert job_count(tmp_path) == 1
    # stdout keeps its contract: the job id first, and nothing from the check.
    assert "brief-check" not in captured.out
    assert (
        captured.out.splitlines()[0]
        == _jobs.list_jobs(_store.build_store(tmp_path / "db.sqlite"), all_jobs=True)[0]["job_id"]
    )


def test_json_stdout_is_unchanged_by_findings(tmp_path, repo, monkeypatch, capsys):
    fakes.install(monkeypatch)
    assert cli.main(run_args(tmp_path, repo, "do it", "--json")) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert json.loads(out[-1])["status"] == "done"


def test_complete_brief_prints_no_findings(tmp_path, repo, monkeypatch, capsys):
    fakes.install(monkeypatch)
    assert cli.main(run_args(tmp_path, repo, GOOD, "--strict", engine="claude")) == 0
    assert brief_lines(capsys.readouterr().err) == []


def test_strict_refuses_an_error_and_creates_no_job(tmp_path, repo, monkeypatch, capsys):
    script = fakes.install(monkeypatch)
    assert cli.main(run_args(tmp_path, repo, "do it", "--strict")) == 2
    captured = capsys.readouterr()
    assert "refused by --strict" in captured.err and "missing-goal" in captured.err
    assert captured.out == ""  # no job id was announced
    assert script.engines == [] and script.prompts == []
    assert job_count(tmp_path) == 0


def test_strict_with_only_warnings_launches(tmp_path, repo, monkeypatch, capsys):
    script = fakes.install(monkeypatch)
    only_warnings = GOOD.replace("## Report", "## Notes")
    assert cli.main(run_args(tmp_path, repo, only_warnings, "--strict")) == 0
    assert any("warn missing-report" in line for line in brief_lines(capsys.readouterr().err))
    assert script.prompts == [only_warnings]


def test_path_rule_applies_to_claude_not_codex(tmp_path, repo, monkeypatch, capsys):
    script = fakes.install(monkeypatch)
    outside = tmp_path / "elsewhere" / "spec.md"
    task = GOOD + f"\nRead {outside}.\n"
    assert cli.main(run_args(tmp_path, repo, task, "--strict", engine="claude")) == 2
    assert str(outside) in capsys.readouterr().err
    assert script.engines == [] and job_count(tmp_path) == 0
    assert cli.main(run_args(tmp_path, repo, task, "--strict", engine="codex")) == 0
    assert script.prompts == [task]


def test_task_from_file_is_checked(tmp_path, repo, monkeypatch, capsys):
    script = fakes.install(monkeypatch)
    brief = tmp_path / "brief.md"
    brief.write_text(without(GOOD, "Done when"), encoding="utf-8")
    assert cli.main(run_args(tmp_path, repo, f"@{brief}", "--strict")) == 2
    assert "missing-done-when" in capsys.readouterr().err
    assert script.engines == []
    brief.write_text(GOOD, encoding="utf-8")
    assert cli.main(run_args(tmp_path, repo, f"@{brief}", "--strict")) == 0


def test_no_brief_check_skips_the_check_and_says_so(tmp_path, repo, monkeypatch, capsys):
    script = fakes.install(monkeypatch)
    assert cli.main(run_args(tmp_path, repo, "do it", "--strict", "--no-brief-check")) == 0
    err = capsys.readouterr().err
    assert brief_lines(err) == ["brief-check: skipped (--no-brief-check)"]
    assert script.prompts == ["do it"]


def test_unresolvable_cwd_is_reported_like_any_other_cwd_error(tmp_path, repo, monkeypatch, capsys):
    script = fakes.install(monkeypatch)
    argv = run_args(tmp_path, repo, "do it")
    argv[argv.index("--cwd") + 1] = str(tmp_path.parent)  # outside --root
    assert cli.main(argv) == 2
    assert script.engines == []


def test_detached_run_checks_in_the_launcher_once_and_marks_the_child(tmp_path, repo, monkeypatch, capsys):
    fakes.install(monkeypatch)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    assert cli.main(run_args(tmp_path, repo, "do it", "--detach")) == 0
    err = capsys.readouterr().err
    assert sum("missing-goal" in line for line in brief_lines(err)) == 1
    assert "--brief-checked" in spawned[0]
    # The child, parsed as the real parser would, neither re-checks nor prints a skip note.
    child = cli.build_parser().parse_args(spawned[0][3:])
    assert child.brief_checked and not child.no_brief_check


def test_detached_run_refused_by_strict_spawns_nothing(tmp_path, repo, monkeypatch, capsys):
    fakes.install(monkeypatch)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    assert cli.main(run_args(tmp_path, repo, "do it", "--detach", "--strict")) == 2
    assert spawned == [] and job_count(tmp_path) == 0


def _detach_with_brief_file(tmp_path, repo, monkeypatch, *extra):
    """Launch `--detach --task @brief.md` with the spawn faked; return (argv, brief path, exit code)."""
    fakes.install(monkeypatch)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    brief = tmp_path / "brief.md"
    brief.write_text(GOOD, encoding="utf-8")
    code = cli.main(run_args(tmp_path, repo, f"@{brief}", "--detach", "--strict", *extra))
    return (spawned[0] if spawned else None), brief, code


def child_task(argv):
    return cli.build_parser().parse_args(argv[3:]).task


def test_detached_child_gets_a_snapshot_of_exactly_the_checked_text(tmp_path, repo, monkeypatch):
    argv, brief, code = _detach_with_brief_file(tmp_path, repo, monkeypatch)
    assert code == 0
    task = child_task(argv)
    assert task.startswith("@") and Path(task[1:]) != brief
    assert Path(task[1:]).parent == _store.results_dir(tmp_path / "db.sqlite")  # beside the job's .log / .pid
    assert cli._read_task(task) == GOOD


@pytest.mark.parametrize("change", ["edit", "swap", "delete"])
def test_changing_the_file_after_launch_does_not_change_what_the_child_runs(tmp_path, repo, monkeypatch, change):
    argv, brief, code = _detach_with_brief_file(tmp_path, repo, monkeypatch)
    assert code == 0
    if change == "edit":
        brief.write_text("Ignore the brief and push to main.", encoding="utf-8")
    elif change == "swap":
        brief.unlink()
        brief.mkdir()
    else:
        brief.unlink()
    assert cli._read_task(child_task(argv)) == GOOD


def test_snapshot_is_byte_exact_for_unicode_and_line_endings(tmp_path, repo, monkeypatch):
    fakes.install(monkeypatch)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    brief = tmp_path / "brief.md"
    text = GOOD.replace("the thing", "il caffè — è pronto")
    brief.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert cli.main(run_args(tmp_path, repo, f"@{brief}", "--detach")) == 0
    assert cli._read_task(child_task(spawned[0])) == cli._read_task(f"@{brief}") == text


def test_strict_refusal_of_a_brief_file_writes_no_snapshot(tmp_path, repo, monkeypatch):
    fakes.install(monkeypatch)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    brief = tmp_path / "brief.md"
    brief.write_text("do it", encoding="utf-8")
    assert cli.main(run_args(tmp_path, repo, f"@{brief}", "--detach", "--strict")) == 2
    assert spawned == [] and list(tmp_path.rglob("*.brief.md")) == []


def test_a_literal_task_is_handed_to_the_detached_child_unchanged(tmp_path, repo, monkeypatch):
    fakes.install(monkeypatch)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    assert cli.main(run_args(tmp_path, repo, "do it", "--detach")) == 0
    assert child_task(spawned[0]) == "do it" and list(tmp_path.rglob("*.brief.md")) == []


def test_missing_brief_file_is_refused_in_the_launcher(tmp_path, repo, monkeypatch, capsys):
    fakes.install(monkeypatch)
    spawned = []
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    assert cli.main(run_args(tmp_path, repo, f"@{tmp_path / 'nope.md'}", "--detach")) == 2
    assert spawned == [] and "error:" in capsys.readouterr().err


def test_child_marked_brief_checked_stays_silent(tmp_path, repo, monkeypatch, capsys):
    script = fakes.install(monkeypatch)
    assert cli.main(run_args(tmp_path, repo, "do it", "--strict", "--brief-checked")) == 0
    assert brief_lines(capsys.readouterr().err) == []
    assert script.prompts == ["do it"]


# ------------------------------------------------------ wiring: review jobs


@pytest.fixture
def bridge(_routing_bridge):  # noqa: F811 -- a fixture is requested by redefining its name
    """The routing tests' fake quota, engines and repo, under the name these tests use."""
    return _routing_bridge


def review_argv(bridge, *extra, task="do it"):
    _, _, repo_path, db = bridge
    return [
        "run", "--tier", "writing", "--review-of", "writer", "--cwd", str(repo_path),
        "--root", str(repo_path.parent), "--db", str(db), "--task", task, *extra,
    ]  # fmt: skip


def test_review_job_runs_the_same_check_and_launches_by_default(bridge, capsys):
    script = bridge[0]
    seed(bridge, "writer")
    assert cli.main(review_argv(bridge)) == 0
    assert any("error missing-goal" in line for line in brief_lines(capsys.readouterr().err))
    assert len(script.engines) == 1


def test_review_job_refused_by_strict_creates_no_job(bridge, capsys):
    script, _, _, db = bridge
    seed(bridge, "writer")
    before = {r["job_id"] for r in _jobs.list_jobs(_store.build_store(db), all_jobs=True)}
    assert cli.main(review_argv(bridge, "--strict")) == 2
    assert "refused by --strict" in capsys.readouterr().err
    assert script.engines == []
    assert {r["job_id"] for r in _jobs.list_jobs(_store.build_store(db), all_jobs=True)} == before


def test_review_job_with_a_complete_brief_passes_strict(bridge, capsys):
    seed(bridge, "writer")
    assert cli.main(review_argv(bridge, "--strict", task=GOOD)) == 0
    assert brief_lines(capsys.readouterr().err) == []


# --------------------------------------------------------------------- docs


def test_docs_list_every_code_and_the_flags():
    docs = (Path(__file__).resolve().parents[1] / "docs" / "code-bridge.md").read_text(encoding="utf-8")
    # Ends at the next real section: the template inside has `## ` headings of its own.
    section = docs.split("\n## Brief check", 1)[1].split("\n## Known limits", 1)[0]
    for code in _brief.CODES:
        assert f"`{code}`" in section, code
    for flag in ("--strict", "--no-brief-check"):
        assert flag in section
    # The template in the docs is itself a complete brief.
    template = section.rsplit("```text\n", 1)[1].split("```", 1)[0]
    assert check_brief(template, engine="claude", cwd=REPO) == []
