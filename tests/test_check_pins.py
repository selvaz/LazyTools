from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_pins", ROOT / "tools" / "check_pins.py")
check_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_pins)

MANIFEST = {
    "verified": {
        "lazybridge": "1.8.1",
        "github": {
            "lazytoolkit": {"repo": "https://github.com/selvaz/LazyTools", "ref": "v0.10.2"},
            "market-data-hub": {"repo": "https://github.com/selvaz/market-data-hub", "ref": "v0.2.0"},
        },
    }
}


def _repo(tmp_path: Path, deps: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\ndependencies = [\n{deps}\n]\n', encoding="utf-8")
    return tmp_path


def test_aligned_repo_passes(tmp_path):
    repo = _repo(
        tmp_path,
        '  "lazybridge>=1.8.1,<2.0",\n'
        '  "lazytoolkit[telegram] @ git+https://github.com/selvaz/LazyTools.git@v0.10.2",\n'
        '  "market-data-hub @ git+https://github.com/selvaz/market-data-hub.git@v0.2.0",',
    )
    assert check_pins.check(repo, MANIFEST) == []


def test_stale_sibling_ref_and_unknown_repo_fail(tmp_path):
    repo = _repo(
        tmp_path,
        '  "market-data-hub @ git+https://github.com/selvaz/market-data-hub.git@636b38a7f9039113b935315fbf5f205ff9180160",\n'
        '  "lazything @ git+https://github.com/selvaz/LazyThing.git@v1.0.0",',
    )
    errors = check_pins.check(repo, MANIFEST)
    assert any("market-data-hub pinned to '636b38a" in e and "v0.2.0" in e for e in errors)
    assert any("LazyThing is not in the ecosystem manifest" in e for e in errors)


def test_lazybridge_exact_pin_and_range_must_admit_the_verified_version(tmp_path):
    repo = _repo(tmp_path, '  "lazybridge==1.8.0",\n  "lazybridge[anthropic]>=1.9,<2.0",')
    errors = check_pins.check(repo, MANIFEST)
    assert any("lazybridge ==1.8.0 excludes the verified 1.8.1" in e for e in errors)
    assert any("<2.0,>=1.9 excludes the verified 1.8.1" in e for e in errors)


def test_lazybridge_name_case_wildcards_and_inline_comments(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "LazyBridge==1.8.0\nlazybridge==1.8.*  # same minor\nlazybridge==1.8.1,!=1.8.1\nlazybridge>=1.9  # minimum\n",
        encoding="utf-8",
    )
    errors = check_pins.check(tmp_path, MANIFEST)
    assert [e.split(":")[1] for e in errors] == ["1", "3", "4"]


def test_requirements_files_and_comments(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "# lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@v0.1.0\n"
        "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@v0.9.0\n"
        "lazybridge==1.8.1\n",
        encoding="utf-8",
    )
    errors = check_pins.check(tmp_path, MANIFEST)
    assert errors == ["requirements.txt:2: LazyTools pinned to 'v0.9.0', manifest says 'v0.10.2'"]


def test_workflow_installs_from_a_moving_branch_fail(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        'run: pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools@main"\n'
        'run: pip install "lazystats @ git+https://github.com/selvaz/LazyStats@main"  # pin-check: ignore\n',
        encoding="utf-8",
    )
    errors = check_pins.check(tmp_path, MANIFEST)
    assert errors == [".github/workflows/ci.yml:1: LazyTools pinned to 'main', manifest says 'v0.10.2'"]


def test_unpinned_git_urls_in_nested_requirement_files_fail(tmp_path):
    deploy = tmp_path / "deploy" / "bot"
    deploy.mkdir(parents=True)
    (deploy / "requirements.txt").write_text(
        "lazytoolkit[telegram] @ git+https://github.com/selvaz/LazyTools.git\n"
        "lazybridge[deepseek] @ git+https://github.com/selvaz/LazyBridge.git\n",
        encoding="utf-8",
    )
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "requirements.txt").write_text(
        "x @ git+https://github.com/selvaz/LazyTools.git\n", encoding="utf-8"
    )
    errors = check_pins.check(tmp_path, MANIFEST)
    assert len(errors) == 2
    assert errors[0].startswith("deploy/bot/requirements.txt:1: LazyTools has no ref") and "use @v0.10.2" in errors[0]
    assert "LazyBridge has no ref" in errors[1] and "PyPI" in errors[1]


def test_a_file_can_opt_out_entirely(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "source-head.yml").write_text(
        '# pin-check: ignore-file\nrun: pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools@main"\n',
        encoding="utf-8",
    )
    assert check_pins.check(tmp_path, MANIFEST) == []
