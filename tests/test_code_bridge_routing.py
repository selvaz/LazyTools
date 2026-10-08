"""Tier routing rules through the real Store/CLI with fake quota and fake engines."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _jobs, _routing, _store, cli
from lazytools.projects.admission import TelemetryReading, WindowReading
from lazytools.routing.catalogue import DEFAULT_PATH, load_tiers


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYBRIDGE_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    script = fakes.install(monkeypatch)
    monkeypatch.setattr(_routing, "load_default_tiers", lambda: load_tiers(DEFAULT_PATH))
    now = datetime.now(UTC)

    def reading(engine, weekly, short):
        return TelemetryReading(
            engine,
            "fake quota",
            now,
            (
                WindowReading(
                    "codex/10080m" if engine == "codex" else "weekly/all models", weekly, 10080, now + timedelta(days=7)
                ),
                WindowReading("codex/300m" if engine == "codex" else "session", short, 300, now + timedelta(hours=5)),
            ),
        )

    readings = {"codex": reading("codex", 10, 20), "claude_code": reading("claude_code", 40, 60)}
    monkeypatch.setattr(_routing, "read_readings", lambda: dict(readings))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    db = tmp_path / "bridge.sqlite"
    return script, readings, repo, db


def args(bridge, command="route", *extra):
    _, _, repo, db = bridge
    result = [command, "--tier", "writing", "--cwd", str(repo), "--db", str(db)]
    if command == "run":
        result += ["--root", str(repo.parent), "--task", "do it"]
    return [*result, *extra]


def seed(bridge, job_id, *, engine="codex", status="done", session=None, created=1, cwd=None):
    _, _, repo, db = bridge
    store = _store.build_store(db)
    _store.build_job_registry(store).write(job_id, "seed", tool_name=engine, status=status)
    _store.write_meta(
        store, job_id, {"engine": engine, "cwd": str(cwd or repo), "session_name": session, "created_at": created}
    )


def test_route_is_dry_run_with_pick_rung_reason_and_both_quota_windows(bridge, capsys):
    script, _, _, db = bridge
    assert cli.main(args(bridge)) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith(
        "chosen: codex/gpt-6.1-sol effort=high tier=writing rung=1 because weekly_margin"
    )
    assert "10% used; reset" in out and "40% used; reset" in out
    assert "20% used; reset" in out and "60% used; reset" in out
    assert "codex quota" in out and "claude quota" in out
    assert script.engines == []
    assert _jobs.list_jobs(_store.build_store(db), all_jobs=True) == []


def test_route_json_is_one_complete_decision(bridge, capsys):
    assert cli.main(args(bridge, "route", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "codex" and payload["engine"] == "codex"
    assert payload["model"] == "gpt-6.1-sol" and payload["effort"] == "high"
    assert payload["rung"] == 1 and payload["override"] == {}
    assert set(payload["scores"]) == {"codex", "claude_code"}
    assert set(payload["readings"]) == {"codex", "claude_code"}


def test_run_launches_selected_model_and_records_decision(bridge, capsys):
    script, _, _, db = bridge
    assert cli.main(args(bridge, "run")) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("chosen: codex/gpt-6.1-sol effort=high")
    store = _store.build_store(db)
    (row,) = _jobs.list_jobs(store, all_jobs=True)
    assert row["model"] == "gpt-6.1-sol" and row["effort"] == "high"
    assert row["routing"]["tier"] == "writing" and row["routing"]["reason"] == "weekly_margin"
    assert row["routing"]["scores"] and row["routing"]["override"] == {}
    assert script.kwargs["model"] == "gpt-6.1-sol"
    assert script.kwargs["reasoning_effort"] == "high"
    assert cli.main(["status", row["job_id"], "--db", str(db)]) == 0
    assert "tier=writing model=gpt-6.1-sol effort=high" in capsys.readouterr().out
    assert cli.main(["jobs", "--all", "--db", str(db)]) == 0
    assert "tier=writing model=gpt-6.1-sol effort=high" in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra,model,effort,override",
    [
        (["--model", "gpt-6-astra"], "gpt-6-astra", "high", {"model": "gpt-6-astra"}),
        (["--effort", "ultra"], "gpt-6.1-sol", "ultra", {"effort": "ultra"}),
        (
            ["--model", " gpt-6-astra ", "--effort", " xhigh "],
            "gpt-6-astra",
            "xhigh",
            {"model": "gpt-6-astra", "effort": "xhigh"},
        ),
        (
            ["--engine", "claude", "--model", "fable", "--effort", "max"],
            "fable",
            "max",
            {"model": "fable", "effort": "max"},
        ),
    ],
)
def test_explicit_overrides_win_and_are_recorded(bridge, capsys, extra, model, effort, override):
    script, _, _, db = bridge
    assert cli.main(args(bridge, "run", *extra, "--json")) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert payload["routing"]["override"] == override
    (row,) = _jobs.list_jobs(_store.build_store(db), all_jobs=True)
    assert (row["model"], row["effort"]) == (model, effort)
    assert script.kwargs["model"] == model
    assert row["routing"]["model"] in ("gpt-6.1-sol", "claude-sonnet-5-5")


@pytest.mark.parametrize(
    "extra", [["--model", "opus"], ["--effort", "none"], ["--engine", "claude", "--effort", "ultra"], ["--model", " "]]
)
def test_bad_routed_overrides_are_rejected_before_launch(bridge, capsys, extra):
    assert cli.main(args(bridge, "run", *extra)) == 2
    assert "REJECTED" in capsys.readouterr().err
    assert bridge[0].engines == []


@pytest.mark.parametrize("engine,provider", [("codex", "codex"), ("claude", "claude_code")])
def test_engine_with_tier_restricts_availability(bridge, capsys, engine, provider):
    assert cli.main(args(bridge, "route", "--engine", engine, "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == provider
    assert "not_available_to_this_agent" in payload["ineligible"].values()


def test_missing_tier_and_engine_is_clear_error(bridge, capsys):
    argv = args(bridge, "run")
    del argv[1:3]
    assert cli.main(argv) == 2
    assert "requires --tier T or --engine E" in capsys.readouterr().err
    assert bridge[0].engines == []


def test_engine_only_keeps_existing_launch_and_never_reads_quota(bridge, monkeypatch, capsys):
    monkeypatch.setattr(_routing, "read_readings", lambda: pytest.fail("read quota in manual mode"))
    argv = args(bridge, "run", "--engine", "claude")
    del argv[1:3]
    assert cli.main(argv) == 0
    _, _, _, db = bridge
    (row,) = _jobs.list_jobs(_store.build_store(db), all_jobs=True)
    assert "routing" not in row and row["model"] is None and row["effort"] is None
    assert capsys.readouterr().out.splitlines()[0] == row["job_id"]


def test_known_session_pins_engine_even_with_better_other_quota(bridge, capsys):
    seed(bridge, "old", engine="claude", session="work")
    assert cli.main(args(bridge, "route", "--session", "work", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "claude_code" and payload["reason"] == "continuity"
    assert payload["ineligible"]["codex"] == "not_available_to_this_agent"


def test_two_consecutive_session_failures_suggest_new_session_without_migrating(bridge, capsys):
    seed(bridge, "ok", engine="claude", session="work", created=1)
    seed(bridge, "bad1", engine="claude", session="work", status="failed", created=2)
    seed(bridge, "bad2", engine="claude", session="work", status="failed", created=3)
    assert cli.main(args(bridge, "route", "--session", "work", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "claude_code" and payload["reason"] == "only_eligible_provider"
    assert "2 consecutive failures" in payload["notice"] and "new --session" in payload["notice"]


def test_success_resets_session_failure_streak_and_other_repo_is_ignored(bridge):
    seed(bridge, "bad1", session="work", status="failed", created=1)
    seed(bridge, "ok", session="work", created=2)
    seed(bridge, "bad2", session="work", status="failed", created=3)
    seed(bridge, "other", engine="claude", session="work", created=4, cwd=bridge[2].parent / "another")
    rows = _jobs.list_jobs(_store.build_store(bridge[3]), all_jobs=True)
    hint = _routing.session_hint(rows, str(bridge[2]), "work")
    assert hint.engine == "codex" and hint.failed_attempts == 1


def test_session_history_in_repo_subdirectory_pins_engine(bridge, capsys):
    sub = bridge[2] / "sub"
    sub.mkdir()
    seed(bridge, "old", engine="claude", session="work", cwd=sub)
    assert cli.main(args(bridge, "route", "--session", "work", "--json")) == 0
    assert json.loads(capsys.readouterr().out)["provider"] == "claude_code"


@pytest.mark.parametrize("extra", [["--engine", "codex"], ["--needs", "images"]])
def test_session_conflict_has_no_pick_and_lists_reasons(bridge, capsys, extra):
    seed(bridge, "old", engine="claude", session="work")
    assert cli.main(args(bridge, "route", "--session", "work", *extra, "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] is None and "--engine" in payload["error"]
    assert payload["ineligible"]


def test_pinned_engine_unreadable_does_not_migrate(bridge, capsys):
    seed(bridge, "old", engine="claude", session="work")
    bridge[1].pop("claude_code")
    assert cli.main(args(bridge, "route", "--session", "work", "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ineligible"]["claude_code"] == "telemetry_missing"
    assert payload["ineligible"]["codex"] == "not_available_to_this_agent"


@pytest.mark.parametrize("writer,reviewer", [("codex", "claude_code"), ("claude", "codex")])
def test_review_uses_opposite_engine_and_accepts_job_prefix(bridge, capsys, writer, reviewer):
    seed(bridge, "writer123", engine=writer)
    assert cli.main(args(bridge, "route", "--review-of", "writer", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == reviewer and "same_as_writer" in payload["ineligible"].values()


@pytest.mark.parametrize("extra", [[], ["--engine", "codex"], ["--needs", "images"], ["--session", "writer-session"]])
def test_review_never_falls_back_to_writer_and_requires_human_when_opposite_ineligible(bridge, capsys, extra):
    seed(bridge, "writer", session="writer-session")
    bridge[1].pop("claude_code")
    assert cli.main(args(bridge, "run", "--review-of", "writer", *extra, "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "human_review_required" and payload["provider"] is None
    assert bridge[0].engines == []


def test_successful_review_run_records_opposite_provider(bridge, capsys):
    seed(bridge, "writer")
    assert cli.main(args(bridge, "run", "--review-of", "writer")) == 0
    rows = _jobs.list_jobs(_store.build_store(bridge[3]), all_jobs=True)
    review = next(row for row in rows if row["job_id"] != "writer")
    assert review["engine"] == "claude" and review["routing"]["ineligible"]["codex"] == "same_as_writer"


def test_unknown_review_job_fails_before_quota_or_launch(bridge, monkeypatch, capsys):
    monkeypatch.setattr(_routing, "read_readings", lambda: pytest.fail("unexpected quota read"))
    assert cli.main(args(bridge, "route", "--review-of", "unknown")) == 2
    assert "no job found" in capsys.readouterr().err


def test_images_require_codex_even_when_claude_has_more_quota(bridge, capsys):
    bridge[1]["codex"] = TelemetryReading(
        "codex", "fixture", datetime.now(UTC), (WindowReading("codex/10080m", 60, 10080),)
    )
    assert cli.main(args(bridge, "route", "--needs", "images", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "codex"
    assert payload["ineligible"]["claude_code"] == "capability_requires_codex"


@pytest.mark.parametrize("mode", ["missing", "exhausted"])
def test_no_eligible_provider_explains_both_engines_and_suggests_manual_engine(bridge, capsys, mode):
    if mode == "missing":
        bridge[1].clear()
    else:
        for engine, reading in bridge[1].items():
            bridge[1][engine] = TelemetryReading(
                engine, reading.source, reading.observed_at, (WindowReading(reading.windows[0].window_id, 100, 10080),)
            )
    assert cli.main(args(bridge, "run")) == 2
    output = capsys.readouterr()
    assert "--engine" in output.err and "no_eligible_provider" in output.err
    assert "excluded codex" in output.out and "excluded claude_code" in output.out
    assert bridge[0].engines == []


def test_in_flight_counts_only_running_bridge_jobs_per_provider(bridge, monkeypatch):
    seed(bridge, "c1", status="running")
    seed(bridge, "c2", status="running", cwd=bridge[2].parent / "elsewhere")
    seed(bridge, "a1", engine="claude", status="running")
    seed(bridge, "approval", engine="claude", status="awaiting_approval")
    seed(bridge, "done")
    original = _routing.recommend
    seen = {}

    def recommend(*a, **kw):
        seen.update(kw)
        return original(*a, **kw)

    monkeypatch.setattr(_routing, "recommend", recommend)
    assert cli.main(args(bridge)) == 0
    assert seen["in_flight"] == {"codex": 2, "claude_code": 1}


def test_detached_run_passes_frozen_selection_to_child_without_recomputing(bridge, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli, "_spawn_detached", lambda argv, log: seen.update(argv=argv) or 1234)
    assert cli.main(args(bridge, "run", "--detach", "--effort", "ultra", "--json")) == 0
    parent = json.loads(capsys.readouterr().out)
    child = seen["argv"][3:]
    assert "--tier" not in child and "--routing-record" in child
    monkeypatch.setattr(_routing, "read_readings", lambda: pytest.fail("recomputed quota in child"))
    assert cli.main(child) == 0
    (row,) = _jobs.list_jobs(_store.build_store(bridge[3]), all_jobs=True)
    assert row["routing"] == parent["routing"]
    assert row["effort"] == "ultra" and row["job_id"] == parent["job_id"]


def test_tiers_path_overrides_packaged_catalogue(bridge, capsys):
    path = bridge[2].parent / "custom.toml"
    path.write_text(DEFAULT_PATH.read_text().replace('effort = "high"', 'effort = "medium"'))
    assert cli.main(args(bridge, "route", "--tiers", str(path), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["effort"] == "medium"


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_catalogue_effort_whitespace_is_normalized_only_for_launch(bridge, capsys, engine):
    path = bridge[2].parent / "spaces.toml"
    path.write_text(DEFAULT_PATH.read_text().replace('effort = "high"', 'effort = " high "'))
    assert cli.main(args(bridge, "run", "--tiers", str(path), "--engine", engine)) == 0
    (row,) = _jobs.list_jobs(_store.build_store(bridge[3]), all_jobs=True)
    assert row["routing"]["effort"] == " high "
    assert row["effort"] == "high" and bridge[0].kwargs["reasoning_effort"] == "high"


def test_route_rejects_missing_cwd(bridge, capsys):
    argv = args(bridge)
    argv[argv.index("--cwd") + 1] = str(bridge[2] / "missing")
    assert cli.main(argv) == 2
    assert "existing directory" in capsys.readouterr().err


def test_run_checks_confinement_before_telemetry(bridge, monkeypatch, capsys):
    monkeypatch.setattr(_routing, "read_readings", lambda: pytest.fail("unexpected quota read"))
    argv = args(bridge, "run")
    argv[argv.index("--root") + 1] = str(bridge[2] / "outside")
    assert cli.main(argv) == 2
    assert "error:" in capsys.readouterr().err


def _forecast_breach_readings(bridge):
    for engine, used in (("codex", 50), ("claude_code", 40)):
        old = bridge[1][engine]
        bridge[1][engine] = TelemetryReading(engine, old.source, old.observed_at, (
            WindowReading("codex/10080m" if engine == "codex" else "weekly/all models", used, 10080,
                          old.observed_at + timedelta(days=5, hours=6)),
        ))


@pytest.mark.parametrize("command", ["route", "run"])
def test_forecast_breaches_choose_more_margin_and_warn_without_blocking(bridge, capsys, command):
    _forecast_breach_readings(bridge)
    assert cli.main(args(bridge, command)) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("chosen: claude/claude-sonnet-5-5 effort=high")
    assert "warning: codex weekly projected 201% (limit 124%) at the current pace" in out
    assert "warning: claude weekly projected 161% (limit 124%) at the current pace" in out
    assert "reset" in out and "; projected 161% (limit 124%)" in out
    assert "excluded" not in out
    if command == "run":
        (row,) = _jobs.list_jobs(_store.build_store(bridge[3]), all_jobs=True)
        assert row["routing"]["operator_directed"] is True
        assert row["routing"]["scores"]["claude_code"]["weekly_margin"] == -37.5
        assert row["routing"]["scores"]["codex"]["weekly_margin"] == -77.5
        assert bridge[0].last.kind == "claude"


def test_bridge_passes_operator_directed_to_recommend(bridge, monkeypatch):
    original = _routing.recommend
    seen = []

    def recommend(*args, **kwargs):
        seen.append(kwargs["operator_directed"])
        return original(*args, **kwargs)

    monkeypatch.setattr(_routing, "recommend", recommend)
    assert cli.main(args(bridge)) == 0
    assert seen == [True]


@pytest.mark.parametrize("command", ["route", "run"])
def test_absolute_ceiling_exclusion_is_still_enforced_by_bridge(bridge, capsys, command):
    _forecast_breach_readings(bridge)
    old = bridge[1]["codex"]
    bridge[1]["codex"] = TelemetryReading("codex", old.source, old.observed_at, (WindowReading("codex/10080m", 94, 10080),))
    assert cli.main(args(bridge, command, "--json")) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    routing = payload["routing"] if command == "run" else payload
    assert routing["provider"] == "claude_code"
    assert routing["ineligible"]["codex"].startswith("admission_would_refuse:absolute_ceiling")


def test_forecast_display_uses_in_flight_reservations_and_observation_time(bridge, capsys):
    _forecast_breach_readings(bridge)
    seed(bridge, "running", status="running")
    assert cli.main(args(bridge)) == 0
    out = capsys.readouterr().out
    assert "warning: codex weekly projected 202% (limit 124%)" in out


def test_unavailable_forecasts_are_labelled_without_warning(bridge, capsys):
    assert cli.main(args(bridge)) == 0
    out = capsys.readouterr().out
    assert "projected unavailable" in out
    assert "warning:" not in out


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5.6-luna"])
@pytest.mark.parametrize("routed", [False, True])
def test_explicit_effort_uses_effective_model_before_any_launch(bridge, capsys, model, routed):
    argv = args(bridge, "run", "--engine", "codex", "--model", model, "--effort", "ultra")
    if not routed:
        del argv[1:3]
    assert cli.main(argv) == 2
    assert model in capsys.readouterr().err
    assert bridge[0].engines == []
    assert _jobs.list_jobs(_store.build_store(bridge[3]), all_jobs=True) == []
    argv[argv.index("ultra")] = " max "
    assert cli.main(argv) == 0
    assert bridge[0].kwargs["reasoning_effort"] == "max"


def test_effort_override_is_checked_against_catalogue_pick(bridge, monkeypatch, capsys):
    catalogue = load_tiers(DEFAULT_PATH)
    from dataclasses import replace

    from lazytools.routing.catalogue import StepModel

    basic = catalogue["basic"]
    catalogue["basic"] = replace(basic, steps=(replace(basic.steps[0], providers=(StepModel("codex", "gpt-6-luna", "high"),)),))
    monkeypatch.setattr(_routing, "load_default_tiers", lambda: catalogue)
    argv = args(bridge, "run", "--effort", "ultra")
    argv[argv.index("writing")] = "basic"
    assert cli.main(argv) == 2
    assert "gpt-6-luna" in capsys.readouterr().err
    assert bridge[0].engines == []


def test_model_override_revalidates_inherited_effort(bridge, monkeypatch, capsys):
    from dataclasses import replace

    catalogue = load_tiers(DEFAULT_PATH)
    writing = catalogue["writing"]
    step = writing.steps[0]
    catalogue["writing"] = replace(writing, steps=(replace(step, providers=(replace(step.providers[0], effort="ultra"),)),))
    monkeypatch.setattr(_routing, "load_default_tiers", lambda: catalogue)
    assert cli.main(args(bridge, "run", "--model", "gpt-6-luna")) == 2
    assert "ultra" in capsys.readouterr().err
    assert bridge[0].engines == []
