"""Tier routing rules through the real Store/CLI with fake quota and fake engines."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import _code_bridge_fakes as fakes
from lazytools.code_bridge import _jobs, _lockfile, _routing, _store, cli
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


def seed(bridge, job_id, *, engine="codex", status="done", session=None, created=1, cwd=None, pid=None):
    _, _, repo, db = bridge
    store = _store.build_store(db)
    _store.build_job_registry(store).write(job_id, "seed", tool_name=engine, status=status)
    _store.write_meta(
        store, job_id, {"engine": engine, "cwd": str(cwd or repo), "session_name": session, "created_at": created,
                        "pid": os.getpid() if pid is None else pid}
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
    if model == "fable":
        reading = bridge[1]["claude_code"]
        bridge[1]["claude_code"] = replace(reading, windows=(*reading.windows, WindowReading("weekly/Fable", 40, 10080)))
    assert cli.main(args(bridge, "run", *extra, "--json")) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert payload["routing"]["override"] == override
    (row,) = _jobs.list_jobs(_store.build_store(db), all_jobs=True)
    assert (row["model"], row["effort"]) == (model, effort)
    assert script.kwargs["model"] == model
    assert row["routing"]["model"] in ("gpt-6.1-sol", "claude-sonnet-5-5")


@pytest.mark.parametrize(
    "extra", [["--engine", "codex", "--model", "opus"], ["--effort", "none"], ["--engine", "claude", "--effort", "ultra"], ["--engine", "codex", "--model", " "]]
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
    assert f"requested engine/model restricts routing to {engine}" in payload["ineligible"].values()


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
    assert "claude" in payload["ineligible"]["codex"]
    assert "work" in payload["ineligible"]["codex"]


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
    assert payload["provider"] is None and payload["reason"] == "session_conflict"
    assert "work" in payload["error"] and "pinned to claude" in payload["error"]
    assert "new --session" in payload["error"]
    assert payload["ineligible"]


def test_pinned_engine_unreadable_does_not_migrate(bridge, capsys):
    seed(bridge, "old", engine="claude", session="work")
    bridge[1].pop("claude_code")
    assert cli.main(args(bridge, "route", "--session", "work", "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ineligible"]["claude_code"] == "telemetry_missing"
    assert payload["ineligible"]["codex"] == "session 'work' is pinned to claude"


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
    assert "no opposite-engine reviewer is eligible" in payload["error"]
    assert "A human review is required" in payload["error"]
    assert "explicit --engine" not in payload["error"]
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
def test_no_eligible_provider_explains_both_engines_with_safe_guidance(bridge, capsys, mode):
    if mode == "missing":
        bridge[1].clear()
    else:
        for engine, reading in bridge[1].items():
            bridge[1][engine] = TelemetryReading(
                engine, reading.source, reading.observed_at, (replace(reading.windows[0], used_percent=100),)
            )
    assert cli.main(args(bridge, "run")) == 2
    output = capsys.readouterr()
    assert "no_eligible_provider" in output.err
    if mode == "missing":
        assert "Try an explicit --engine" in output.err
    else:
        assert "--engine" not in output.err
        assert "Wait for quota to reset" in output.err
        for reading in bridge[1].values():
            window = reading.windows[0]
            assert f"{window.window_id} at 100%" in output.err
            assert window.resets_at.isoformat() in output.err
    assert "codex:" in output.err and "claude_code:" in output.err
    assert output.err.count("codex:") == 1 and output.err.count("claude_code:") == 1
    assert "excluded" not in output.out
    assert bridge[0].engines == []


def test_in_flight_counts_live_running_and_approval_waiting_jobs_per_provider(bridge, monkeypatch):
    """A job paused on an approval ticket resumes without another admission check, so it keeps
    its reservation (Codex review on #186); finished jobs and other repos do not count."""
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
    assert seen["in_flight"] == {"codex": 2, "claude_code": 2}


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


def _short_windows(bridge, *, codex_weekly=40, codex_short=0, claude_weekly=10, claude_short=100):
    for engine, weekly, short in (("codex", codex_weekly, codex_short), ("claude_code", claude_weekly, claude_short)):
        old = bridge[1][engine]
        bridge[1][engine] = replace(old, windows=(
            WindowReading(old.windows[0].window_id, weekly, 10080),
            WindowReading("codex/300m" if engine == "codex" else "session", short, 300),
        ))


@pytest.mark.parametrize("command", ["route", "run"])
@pytest.mark.parametrize("exhausted,chosen", [("claude_code", "codex"), ("codex", "claude_code")])
@pytest.mark.parametrize("used", [94, 95, 100])
def test_full_admission_excludes_short_window_and_reroutes_without_changing_router(bridge, capsys, command, exhausted, chosen, used):
    from lazytools.projects.admission import budget_for
    from lazytools.routing import route

    _short_windows(bridge, codex_weekly=10 if exhausted == "codex" else 40,
                   codex_short=used if exhausted == "codex" else 0,
                   claude_weekly=10 if exhausted == "claude_code" else 40,
                   claude_short=used if exhausted == "claude_code" else 0)
    # The original pure router still makes exactly its weekly-only pick.
    pure = route("writing", catalogue=load_tiers(DEFAULT_PATH), readings=bridge[1],
                 budgets={engine: budget_for(engine) for engine in bridge[1]}, in_flight={}, operator_directed=True)
    assert pure.provider == exhausted
    assert cli.main(args(bridge, command, "--json")) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    routing = payload["routing"] if command == "run" else payload
    assert routing["provider"] == chosen
    assert routing["ineligible"][exhausted].startswith("bridge_admission_would_refuse:absolute_ceiling")
    assert "session" in routing["ineligible"][exhausted] if exhausted == "claude_code" else "codex/300m" in routing["ineligible"][exhausted]
    assert exhausted in routing["scores"]  # The reason for rejecting the initial pick remains auditable.
    if command == "run":
        assert bridge[0].last.kind == _routing.bridge_engine(chosen)


@pytest.mark.parametrize("command", ["route", "run"])
def test_both_short_windows_exhausted_refuse_and_warn_without_forecast(bridge, capsys, command):
    _short_windows(bridge, codex_short=100, claude_short=100)
    assert cli.main(args(bridge, command)) == 2
    captured = capsys.readouterr()
    assert "warning: codex 5-hour (codex/300m) at 100% used" in captured.out
    assert "warning: claude 5-hour (session) at 100% used" in captured.out
    assert "projected unavailable" in captured.out and "weekly projected" not in captured.out
    assert captured.err.count("codex:") == 1 and captured.err.count("claude_code:") == 1
    assert "bridge_admission_would_refuse:absolute_ceiling" in captured.err
    assert "--engine" not in captured.err and "reset unknown" in captured.err
    assert bridge[0].engines == []


def test_review_short_ceiling_requires_human_and_never_uses_writer(bridge, capsys):
    seed(bridge, "writer", engine="codex")
    _short_windows(bridge)
    assert cli.main(args(bridge, "run", "--review-of", "writer", "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "human_review_required"
    assert "no opposite-engine reviewer is eligible" in payload["error"]
    assert "session at 100%" in payload["error"] and "A human review is required" in payload["error"]
    assert "explicit --engine" not in payload["error"] and bridge[0].engines == []


def test_unclassified_window_is_displayed_but_does_not_gate_a_model(bridge, capsys):
    _short_windows(bridge, claude_short=0)
    old = bridge[1]["claude_code"]
    bridge[1]["claude_code"] = replace(old, windows=(*old.windows, WindowReading("custom", 100)))
    assert cli.main(args(bridge)) == 0
    out = capsys.readouterr().out
    assert out.startswith("chosen: claude/")
    assert "warning: claude other (custom) at 100% used" in out
    assert "bridge_admission_would_refuse:absolute_ceiling" not in out


@pytest.mark.parametrize("command,model", [
    ("route", None), ("run", None), ("run", "claude-sonnet-5-5"),
    ("run", "claude-opus-5-5"), ("run", "sonnet"), ("run", "opus"),
])
@pytest.mark.parametrize("engine", [None, "claude"])
def test_exhausted_fable_bucket_does_not_block_sonnet_or_opus(bridge, capsys, command, model, engine):
    _short_windows(bridge, claude_short=5)
    reading = bridge[1]["claude_code"]
    bridge[1]["claude_code"] = replace(reading, windows=(*reading.windows, WindowReading("weekly/Fable", 97, 10080)))
    extra = (["--model", model] if model else []) + (["--engine", engine] if engine else [])
    assert cli.main(args(bridge, command, *extra, "--json")) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    routing = payload["routing"] if command == "run" else payload
    assert routing["provider"] == "claude_code"
    assert "claude_code" not in routing["ineligible"]
    if command == "run":
        (row,) = _jobs.list_jobs(_store.build_store(bridge[3]), all_jobs=True)
        assert row["model"] == bridge[0].kwargs["model"] == (model or "claude-sonnet-5-5")
    else:
        assert payload["model"] == "claude-sonnet-5-5"


def test_catalogue_opus_route_ignores_fable_bucket(bridge, capsys):
    _short_windows(bridge, claude_short=5)
    reading = bridge[1]["claude_code"]
    bridge[1]["claude_code"] = replace(reading, windows=(*reading.windows, WindowReading("weekly/Fable", 97, 10080)))
    path = bridge[2].parent / "opus.toml"
    path.write_text(DEFAULT_PATH.read_text().replace("claude-sonnet-5-5", "claude-opus-5-5"))
    assert cli.main(args(bridge, "route", "--engine", "claude", "--tiers", str(path), "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "claude_code" and payload["model"] == "claude-opus-5-5"


@pytest.mark.parametrize("model", ["fable", " claude-fable-5-5 "])
def test_explicit_fable_pick_is_blocked_by_its_own_weekly_bucket(bridge, capsys, model):
    _short_windows(bridge, claude_short=5)
    reading = bridge[1]["claude_code"]
    reset = reading.observed_at + timedelta(days=2)
    bridge[1]["claude_code"] = replace(reading, windows=(*reading.windows, WindowReading("weekly/Fable", 97, 10080, reset)))
    assert cli.main(args(bridge, "run", "--model", model, "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] is None
    assert "admission_would_refuse:absolute_ceiling" in payload["ineligible"]["claude_code"]
    assert "weekly/Fable at 97%" in payload["error"] and reset.isoformat() in payload["error"]
    assert "At ceiling: claude weekly/Fable" in payload["error"]
    assert "weekly/all models at" not in payload["error"]
    assert "--engine" not in payload["error"] and bridge[0].engines == []


def test_fable_override_requires_its_own_weekly_telemetry(bridge, capsys):
    assert cli.main(args(bridge, "run", "--model", "claude-fable-5-5", "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "no usable weekly window" in payload["ineligible"]["claude_code"]
    assert "'fable'" in payload["ineligible"]["claude_code"]
    assert bridge[0].engines == []


@pytest.mark.parametrize("command", ["route", "run"])
@pytest.mark.parametrize("extra_bucket", ["gpt-6-astra", "codex-gpt-6-astra", "codex/gpt-6-astra"])
@pytest.mark.parametrize("order", ["before", "after"])
def test_codex_other_model_limit_buckets_do_not_block_the_pick(bridge, capsys, command, extra_bucket, order):
    reading = bridge[1]["codex"]
    extra = (WindowReading(f"{extra_bucket}/10080m", 100, 10080),
        WindowReading(f"{extra_bucket}/300m", 100, 300),
    )
    windows = (*extra, *reading.windows) if order == "before" else (*reading.windows, *extra)
    bridge[1]["codex"] = replace(reading, windows=windows)
    assert cli.main(args(bridge, command, "--engine", "codex", "--json")) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    routing = payload["routing"] if command == "run" else payload
    assert routing["provider"] == "codex" and "codex" not in routing["ineligible"]


@pytest.mark.parametrize("model", ["fable", "claude-fable-5-5"])
def test_fable_override_uses_its_own_healthy_bucket_when_all_models_is_exhausted(bridge, capsys, model):
    reading = bridge[1]["claude_code"]
    bridge[1]["claude_code"] = replace(reading, windows=(
        replace(reading.windows[0], used_percent=100), replace(reading.windows[1], used_percent=5),
        WindowReading("weekly/Fable", 10, 10080),
    ))
    assert cli.main(args(bridge, "run", "--model", model, "--json")) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert payload["routing"]["provider"] == "claude_code"
    assert payload["routing"]["model"] == "claude-sonnet-5-5"
    assert payload["routing"]["override"] == {"model": model}
    assert payload["routing"]["scores"]["claude_code"]["weekly_position_margin"] == 65
    assert bridge[0].kwargs["model"] == model


@pytest.mark.parametrize("reviewer", ["claude_code", "codex"])
@pytest.mark.parametrize("exhausted", ["weekly", "both"])
def test_review_ceiling_error_includes_applicable_windows_and_resets(bridge, capsys, reviewer, exhausted):
    seed(bridge, "writer", engine="codex" if reviewer == "claude_code" else "claude")
    reading = bridge[1][reviewer]
    weekly = replace(reading.windows[0], used_percent=100)
    short = replace(reading.windows[1], used_percent=100 if exhausted == "both" else 5)
    bridge[1][reviewer] = replace(reading, windows=(weekly, short))
    assert cli.main(args(bridge, "run", "--review-of", "writer", "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "human_review_required"
    assert "A human review is required" in payload["error"]
    assert "--engine" not in payload["error"]
    for window in (weekly, short) if exhausted == "both" else (weekly,):
        assert f"{window.window_id} at 100%" in payload["error"]
        assert window.resets_at.isoformat() in payload["error"]
    assert bridge[0].engines == []


@pytest.mark.parametrize("engine", ["claude_code", "codex"])
def test_post_pick_admission_receives_only_applicable_windows_and_preserves_reading(bridge, monkeypatch, engine):
    reading = bridge[1][engine]
    bridge[1][engine] = replace(reading, windows=(*reading.windows, WindowReading("weekly/Fable", 97, 10080)))
    seen = []
    original = _routing.decide

    def decide(scoped, budget, **kwargs):
        seen.append(scoped)
        return original(scoped, budget, **kwargs)

    monkeypatch.setattr(_routing, "decide", decide)
    selection = _routing.choose("writing", cwd=str(bridge[2]), db_path=bridge[3], engine=_routing.bridge_engine(engine))
    assert selection.decision.provider == engine
    (scoped,) = seen
    assert scoped == reading  # Its source, timestamp and applicable windows survive.
    assert selection.readings[engine] == bridge[1][engine]  # Reporting retains raw telemetry.


@pytest.mark.parametrize("engine", ["codex", "claude"])
@pytest.mark.parametrize("command", ["route", "run"])
def test_ceiling_error_lists_all_applicable_windows_and_resets_without_manual_hint(bridge, capsys, engine, command):
    native = _routing.provider(engine)
    reading = bridge[1][native]
    weekly, short = (replace(window, used_percent=100) for window in reading.windows)
    unrelated = WindowReading("weekly/Fable" if native == "claude_code" else "codex-other/10080m", 100, 10080)
    bridge[1][native] = replace(reading, windows=(weekly, short, unrelated))
    assert cli.main(args(bridge, command, "--engine", engine, "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    error = payload["error"]
    assert "Wait for quota to reset" in error and "--engine" not in error
    for window in (weekly, short):
        assert f"{window.window_id} at 100%" in error
        assert window.resets_at.isoformat() in error
    assert unrelated.window_id not in error
    assert bridge[0].engines == []


@pytest.mark.parametrize("restriction", [["--engine", "claude"], ["--model", "claude-opus-5-5"]])
def test_requested_engine_failure_does_not_suggest_requesting_an_engine_again(bridge, capsys, restriction):
    bridge[1].clear()
    assert cli.main(args(bridge, "run", *restriction, "--json")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "telemetry_missing" in payload["error"]
    assert "Try an explicit --engine" not in payload["error"]


@pytest.mark.parametrize("command", ["route", "run"])
@pytest.mark.parametrize("json_output", [False, True])
def test_images_with_explicit_claude_and_pinned_session_reports_capability_conflict(bridge, monkeypatch, capsys, command, json_output):
    seed(bridge, "old", engine="claude", session="conversation")
    monkeypatch.setattr(_routing, "read_readings", lambda: pytest.fail("read quota despite capability/session conflict"))
    extra = ["--engine", "claude", "--session", "conversation", "--needs", "images"]
    assert cli.main(args(bridge, command, *extra, *(["--json"] if json_output else []))) == 2
    captured = capsys.readouterr()
    error = json.loads(captured.out)["error"] if json_output else captured.err
    assert "images need codex" in error
    assert "Session 'conversation' is pinned to claude" in error
    assert "new --session" in error and "the requested codex engine" not in error
    assert bridge[0].engines == []


def test_real_failure_causes_reach_human_error_once_per_engine(bridge, monkeypatch, capsys):
    from lazytools.routing import live

    monkeypatch.setenv(live.CACHE_PATH_ENV, str(bridge[2].parent / "quota.json"))
    monkeypatch.setattr(_routing, "read_readings", live.read_readings)

    def read(engine, **kwargs):
        return TelemetryReading(engine, "fake", datetime.now(UTC), error="timed out after 45s" if engine == "codex" else "not logged in")

    monkeypatch.setattr(live.quota_telemetry, "read_quota_sync", read)
    assert cli.main(args(bridge)) == 2
    captured = capsys.readouterr()
    assert captured.err.count("timed out after 45s") == 1
    assert captured.err.count("not logged in") == 1
    assert "telemetry_missing" not in captured.err and "excluded" not in captured.out


def test_dead_running_jobs_do_not_reserve_quota(bridge, monkeypatch):
    for job_id, engine, pid in (("live", "codex", 101), ("dead", "codex", 102), ("dead-claude", "claude", 103),
                                 ("zero", "claude", 0), ("invalid", "claude", "104")):
        seed(bridge, job_id, engine=engine, status="running", pid=pid)
    seen = []
    monkeypatch.setattr(_lockfile, "_pid_alive", lambda pid: seen.append(pid) or pid == 101)
    selection = _routing.choose("writing", cwd=str(bridge[2]), db_path=bridge[3])
    assert selection.in_flight == {"codex": 1, "claude_code": 0}
    assert set(seen) == {0, 101, 102, 103} and 104 not in seen


@pytest.mark.parametrize("pinned,requested", [("claude", "codex"), ("codex", "claude")])
@pytest.mark.parametrize("routed,detached", [(False, False), (False, True), (True, False), (True, True)])
def test_manual_and_tier_session_conflicts_use_same_message_before_launch(bridge, monkeypatch, capsys, pinned, requested, routed, detached):
    seed(bridge, "old", engine=pinned, session="conversation")
    monkeypatch.setattr(_routing, "read_readings", lambda: pytest.fail("read quota despite session conflict"))
    monkeypatch.setattr(cli, "_spawn_detached", lambda *a: pytest.fail("spawned conflicting session"))
    argv = args(bridge, "run", "--engine", requested, "--session", "conversation")
    if not routed:
        del argv[1:3]
    if detached:
        argv.append("--detach")
    assert cli.main(argv) == 2
    error = capsys.readouterr().err
    expected = _routing.session_conflict_message("conversation", _routing.provider(pinned), _routing.provider(requested))
    assert error.strip() == "error: " + expected
    assert bridge[0].engines == []


@pytest.mark.parametrize("model,chosen", [("claude-sonnet-5-5", "claude_code"), ("claude-opus-5-5", "claude_code"),
                                          ("sonnet", "claude_code"), ("gpt-6-astra", "codex"), ("gpt-6-luna", "codex")])
def test_model_without_engine_restricts_routing_before_pick(bridge, capsys, model, chosen):
    _short_windows(bridge, codex_weekly=60 if chosen == "codex" else 10, claude_weekly=60 if chosen == "claude_code" else 10,
                   codex_short=0, claude_short=0)
    assert cli.main(args(bridge, "run", "--model", model, "--json")) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert payload["routing"]["provider"] == chosen
    assert payload["routing"]["override"]["model"] == model
    assert bridge[0].kwargs["model"] == model


def test_inferred_model_engine_cannot_escape_session_pin(bridge, monkeypatch, capsys):
    seed(bridge, "old", engine="claude", session="work")
    monkeypatch.setattr(_routing, "read_readings", lambda: pytest.fail("quota despite session conflict"))
    assert cli.main(args(bridge, "run", "--model", "gpt-6-astra", "--session", "work")) == 2
    assert "session_conflict" in capsys.readouterr().err and bridge[0].engines == []


def test_distinct_rung_errors_are_grouped_under_one_engine_label(bridge, capsys):
    for engine, old in bridge[1].items():
        bridge[1][engine] = replace(old, windows=(old.windows[1],))
    assert cli.main(args(bridge)) == 2
    error = capsys.readouterr().err
    assert error.count("codex:") == 1 and error.count("claude_code:") == 1
    assert "claude-sonnet-5-5" in error and "claude-opus-5-5" in error


def test_route_accepts_the_same_model_and_effort_overrides_as_run(bridge, monkeypatch):
    seen = {}
    original = _routing.choose

    def choose(*a, **kw):
        seen.update(kw)
        return original(*a, **kw)

    monkeypatch.setattr(_routing, "choose", choose)
    cli.main(["route", "--tier", "writing", "--model", "claude-opus-5-5", "--effort", "medium",
              "--cwd", str(bridge[2]), "--db", str(bridge[3])])
    assert seen["model"] == "claude-opus-5-5" and seen["effort"] == "medium"
