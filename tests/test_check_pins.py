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


def test_stale_sibling_ref_fails_and_unknown_repo_warns(tmp_path):
    repo = _repo(
        tmp_path,
        '  "market-data-hub @ git+https://github.com/selvaz/market-data-hub.git@636b38a7f9039113b935315fbf5f205ff9180160",\n'
        '  "lazything @ git+https://github.com/selvaz/LazyThing.git@v1.0.0",',
    )
    warnings = []
    errors = check_pins.check(repo, MANIFEST, warnings)
    assert any("market-data-hub pinned to '636b38a" in e and "v0.2.0" in e for e in errors)
    assert warnings == ["pyproject.toml:5: LazyThing is unmanaged (not in the ecosystem manifest)"]


def test_ref_matching_does_not_accept_a_valid_prefix(tmp_path):
    repo = _repo(
        tmp_path,
        '  "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@v0.10.2+stale",',
    )

    assert check_pins.check(repo, MANIFEST) == [
        "pyproject.toml:4: LazyTools pinned to 'v0.10.2+stale', manifest says 'v0.10.2'"
    ]


def test_unmanaged_installer_repo_warns_and_exits_zero(tmp_path, capsys):
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    (manifest_dir / "install.toml").write_text(
        'source = "git+https://github.com/selvaz/LazyStudio.git@0a49728"\n', encoding="utf-8"
    )
    manifest = tmp_path / "compatibility.toml"
    manifest.write_text(
        '[verified]\nlazybridge = "1.8.1"\n[verified.github.lazytoolkit]\n'
        'repo = "https://github.com/selvaz/LazyTools"\nref = "v0.10.2"\n',
        encoding="utf-8",
    )

    assert check_pins.main(["--repo", str(tmp_path), "--manifest", str(manifest)]) == 0
    captured = capsys.readouterr()
    assert "warning: manifest/install.toml:1: LazyStudio is unmanaged" in captured.err
    assert "ecosystem pins match the manifest" in captured.out


def test_nested_pyproject_drift_fails(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    _repo(backend, '  "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@v0.10.4",')

    assert check_pins.check(tmp_path, MANIFEST) == [
        "backend/pyproject.toml:4: LazyTools pinned to 'v0.10.4', manifest says 'v0.10.2'"
    ]


def test_lazy_installer_and_lazystudio_baseline_drifts_are_all_caught(tmp_path):
    """Model lazy-installer 6cd5cd5 and LazyStudio baseline-phase1 0a49728."""
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    (manifest_dir / "default.toml").write_text(
        'tools = "git+https://github.com/selvaz/LazyTools.git@v0.10.4"\n'
        'ceo = "git+https://github.com/selvaz/LazyCEO.git@v0.2.7"\n'
        'data = "git+https://github.com/selvaz/market-data-hub.git@v0.3.9"\n'
        'bridge = "lazybridge==1.8.3"\n'
        'studio = "git+https://github.com/selvaz/LazyStudio.git@0a49728"\n',
        encoding="utf-8",
    )
    backend = tmp_path / "LazyStudio" / "backend"
    backend.mkdir(parents=True)
    _repo(
        backend,
        '  "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@v0.10.4",\n'
        '  "lazyceo @ git+https://github.com/selvaz/LazyCEO.git@v0.2.7",',
    )
    release = {
        "verified": {
            "lazybridge": "1.8.7",
            "github": {
                "lazytoolkit": {"repo": "https://github.com/selvaz/LazyTools", "ref": "v0.10.6"},
                "lazyceo": {"repo": "https://github.com/selvaz/LazyCEO", "ref": "v0.2.10"},
                "market-data-hub": {
                    "repo": "https://github.com/selvaz/market-data-hub",
                    "ref": "v0.3.29",
                },
            },
        }
    }
    warnings = []

    errors = check_pins.check(tmp_path, release, warnings)

    assert len(errors) == 6
    assert {"v0.10.4", "v0.2.7", "v0.3.9", "==1.8.3"} <= {
        token for error in errors for token in error.replace("'", " ").split()
    }
    assert warnings == ["manifest/default.toml:5: LazyStudio is unmanaged (not in the ecosystem manifest)"]


def test_nested_aligned_projects_and_manifest_pass(tmp_path):
    (tmp_path / "backend").mkdir()
    _repo(
        tmp_path / "backend",
        '  "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@v0.10.2",\n'
        '  "lazybridge==1.8.1",',
    )
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    (manifest_dir / "default.toml").write_text(
        'tools = "git+https://github.com/selvaz/LazyTools.git@v0.10.2"\nbridge = "lazybridge==1.8.1"\n',
        encoding="utf-8",
    )
    assert check_pins.check(tmp_path, MANIFEST) == []


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


def test_all_txt_files_below_requirements_directories_are_scanned(tmp_path):
    requirements = tmp_path / "deploy" / "requirements"
    requirements.mkdir(parents=True)
    (requirements / "verified.txt").write_text(
        "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@v0.9.0\n",
        encoding="utf-8",
    )

    assert check_pins.check(tmp_path, MANIFEST) == [
        "deploy/requirements/verified.txt:1: LazyTools pinned to 'v0.9.0', manifest says 'v0.10.2'"
    ]


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


def test_git_url_variants_are_case_insensitive_and_checked(tmp_path):
    repo = _repo(
        tmp_path,
        '  "x @ GIT+SSH://git@github.com/Selvaz/LazyTools.git@old",\n'
        '  "x @ git@github.com:selvaz/LazyTools.git@old",\n'
        '  "x @ git+git://secret-token@GITHUB.COM/SElVaZ/LazyTools.git@old",',
    )

    errors = check_pins.check(repo, MANIFEST)

    assert len(errors) == 3
    assert all("LazyTools pinned to 'old'" in error for error in errors)
    assert all("secret-token" not in error for error in errors)


def test_archive_ref_is_checked(tmp_path):
    repo = _repo(tmp_path, '  "https://github.com/selvaz/LazyTools/archive/refs/tags/v0.9.0.tar.gz",')

    assert check_pins.check(repo, MANIFEST) == [
        "pyproject.toml:4: LazyTools pinned to 'v0.9.0', manifest says 'v0.10.2'"
    ]


def test_build_named_source_project_is_scanned_but_build_outputs_are_not(tmp_path):
    backend_build = tmp_path / "backend" / "build"
    backend_build.mkdir(parents=True)
    (backend_build / "requirements.txt").write_text(
        "x @ git+https://github.com/selvaz/LazyTools@old\n", encoding="utf-8"
    )
    (tmp_path / "package").mkdir()
    _repo(tmp_path / "package", "")
    generated = tmp_path / "package" / "build" / "lib"
    generated.mkdir(parents=True)
    (generated / "requirements.txt").write_text(
        "x @ git+https://github.com/selvaz/LazyTools@also-old\n", encoding="utf-8"
    )

    assert check_pins.check(tmp_path, MANIFEST) == [
        "backend/build/requirements.txt:1: LazyTools pinned to 'old', manifest says 'v0.10.2'"
    ]


def test_nested_workflow_is_scanned(tmp_path):
    workflow = tmp_path / "project" / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yaml").write_text(
        "run: pip install git+https://github.com/selvaz/LazyTools@old\n", encoding="utf-8"
    )

    assert check_pins.check(tmp_path, MANIFEST) == [
        "project/.github/workflows/ci.yaml:1: LazyTools pinned to 'old', manifest says 'v0.10.2'"
    ]


def test_templated_ci_ref_warns_instead_of_failing(tmp_path):
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "release.yml").write_text(
        'run: pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@${{ github.ref_name }}"\n',
        encoding="utf-8",
    )
    warnings = []

    assert check_pins.check(tmp_path, MANIFEST, warnings) == []
    assert warnings == [".github/workflows/release.yml:1: LazyTools uses a templated ref; not checked"]


def test_shell_ref_literal_default_is_checked(tmp_path):
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "release.yml").write_text(
        'run: pip install "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git@${REF:-v0.9.0}"\n',
        encoding="utf-8",
    )
    warnings = []

    assert check_pins.check(tmp_path, MANIFEST, warnings) == [
        ".github/workflows/release.yml:1: LazyTools pinned to 'v0.9.0', manifest says 'v0.10.2'"
    ]
    assert warnings == []


def test_unparseable_selvaz_url_warns_without_leaking_userinfo(tmp_path):
    repo = _repo(tmp_path, '  "x @ git+https://secret-token@github.com/selvaz?repo=LazyTools&ref=old",')
    warnings = []

    assert check_pins.check(repo, MANIFEST, warnings) == []
    assert warnings == ["pyproject.toml:4: unrecognised selvaz GitHub URL; pin could not be checked"]
    assert "secret-token" not in warnings[0]


def test_candidate_release_set_is_accepted_whole(tmp_path):
    main_toml = tmp_path / "main.toml"
    cand_toml = tmp_path / "cand.toml"
    body = '[verified]\nlazybridge = "1.8.1"\n[verified.github.market-data-hub]\nrepo = "https://github.com/selvaz/market-data-hub"\nref = "{ref}"\n[verified.github.lazytoolkit]\nrepo = "https://github.com/selvaz/LazyTools"\nref = "{tk}"\n'
    main_toml.write_text(body.format(ref="v0.2.0", tk="v0.10.2"), encoding="utf-8")
    cand_toml.write_text(body.format(ref="v0.2.1", tk="v0.10.2"), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text(
        "market-data-hub @ git+https://github.com/selvaz/market-data-hub.git@v0.2.1\n", encoding="utf-8"
    )
    args = ["--repo", str(repo), "--manifest", str(main_toml), "--candidate", str(cand_toml)]
    assert check_pins.main(args) == 0
    (repo / "requirements.txt").write_text(
        "market-data-hub @ git+https://github.com/selvaz/market-data-hub.git@v0.1.9\n", encoding="utf-8"
    )
    assert check_pins.main(args) == 1
