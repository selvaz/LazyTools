"""Live adapter tests: all provider reads are faked, including timeout failures."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from lazytools.projects.admission import TelemetryReading, WindowReading
from lazytools.routing import live, load_default_tiers, recommend

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def reading(engine, *, observed_at=NOW, error=None):
    bucket = "codex/10080m" if engine == "codex" else "weekly/all models"
    return TelemetryReading(engine, "fake", observed_at, (WindowReading(bucket, 10, 10080),), error)


def fake_reader(monkeypatch, *, observed_at=NOW, failed=()):
    calls = []

    def read(engine, *, timeout):
        calls.append((engine, timeout))
        return reading(engine, observed_at=observed_at, error="timeout" if engine in failed else None)

    monkeypatch.setattr(live.quota_telemetry, "read_quota_sync", read)
    return calls


def test_file_cache_is_reused_across_calls_without_memory_cache(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    calls = fake_reader(monkeypatch)
    first = live.read_readings(cache_path=path, timeout=0.1, now=NOW)
    second = live.read_readings(cache_path=path, now=NOW + timedelta(seconds=120))
    assert first == second
    assert calls == [("codex", 0.1), ("claude_code", 0.1)]
    assert json.loads(path.read_text())["version"] == 1


def test_expired_cache_is_refreshed_per_engine(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    calls = fake_reader(monkeypatch)
    live.read_readings(cache_path=path, now=NOW)
    calls.clear()
    moment = NOW + timedelta(seconds=121)
    fake_reader(monkeypatch, observed_at=moment)
    refreshed = live.read_readings(cache_path=path, now=moment)
    assert all(value.observed_at == moment for value in refreshed.values())


@pytest.mark.parametrize(
    "content", [None, "broken JSON", "[]", '{"version":1,"readings":[]}', '{"version":1,"readings":{"codex":null}}']
)
def test_missing_or_corrupt_cache_never_prevents_live_reads(tmp_path, monkeypatch, content):
    path = tmp_path / "cache.json"
    if content is not None:
        path.write_text(content)
    calls = fake_reader(monkeypatch)
    assert len(live.read_readings(cache_path=path, now=NOW)) == 2
    assert len(calls) == 2


@pytest.mark.parametrize("mutation", ["future", "naive", "nan", "negative", "wrong_engine", "bad_duration"])
def test_malformed_entry_is_refetched_without_discarding_other_engine(tmp_path, monkeypatch, mutation):
    path = tmp_path / "cache.json"
    fake_reader(monkeypatch)
    live.read_readings(cache_path=path, now=NOW)
    data = json.loads(path.read_text())
    entry = data["readings"]["codex"]
    if mutation == "future":
        entry["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif mutation == "naive":
        entry["observed_at"] = NOW.replace(tzinfo=None).isoformat()
    elif mutation == "wrong_engine":
        entry["engine"] = "claude_code"
    elif mutation == "bad_duration":
        entry["windows"][0]["duration_minutes"] = "weekly"
    else:
        entry["windows"][0]["used_percent"] = float("nan") if mutation == "nan" else -1
    path.write_text(json.dumps(data))
    calls = fake_reader(monkeypatch)
    assert len(live.read_readings(cache_path=path, now=NOW)) == 2
    assert [engine for engine, _ in calls] == ["codex"]


def test_failed_read_is_missing_and_not_cached_or_replaced_by_stale_data(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    fake_reader(monkeypatch)
    live.read_readings(cache_path=path, now=NOW)
    fake_reader(monkeypatch, observed_at=NOW + timedelta(seconds=121), failed=("codex",))
    readings = live.read_readings(cache_path=path, now=NOW + timedelta(seconds=121))
    assert set(readings) == {"claude_code"}
    assert "codex" not in json.loads(path.read_text())["readings"]


def test_reader_exception_does_not_escape_recommend(tmp_path, monkeypatch):
    monkeypatch.setenv(live.CACHE_PATH_ENV, str(tmp_path / "cache.json"))
    monkeypatch.setattr(
        live.quota_telemetry, "read_quota_sync", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    decision = recommend("writing", catalogue=load_default_tiers(), in_flight={}, now=NOW)
    assert decision.provider is None
    assert decision.ineligible["codex"] == "telemetry_missing"
    assert decision.ineligible["claude_code"] == "telemetry_missing"


def test_explicit_empty_readings_bypasses_telemetry(monkeypatch):
    monkeypatch.setattr(live, "read_readings", lambda **kw: pytest.fail("unexpected live read"))
    assert recommend("writing", catalogue=load_default_tiers(), in_flight={}, readings={}, now=NOW).provider is None


def test_recommend_uses_admission_budget_for_each_engine(monkeypatch):
    from lazytools.projects.admission import EngineBudget

    seen = []

    def budget(engine):
        seen.append(engine)
        return EngineBudget(engine, ceiling_percent=10)

    monkeypatch.setattr(live, "budget_for", budget)
    decision = recommend(
        "writing", catalogue=load_default_tiers(), in_flight={}, readings={"codex": reading("codex")}, now=NOW
    )
    assert seen == ["codex", "claude_code"]
    assert decision.provider is None
    assert "admission_would_refuse" in decision.ineligible["codex"]


def test_atomic_write_preserves_previous_file_if_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    path.write_text("previous")
    monkeypatch.setattr(live.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    fake_reader(monkeypatch)
    assert len(live.read_readings(cache_path=path, now=NOW)) == 2
    assert path.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [path]


def test_unwritable_cache_is_nonfatal(tmp_path, monkeypatch):
    path = tmp_path / "is-a-directory"
    path.mkdir()
    fake_reader(monkeypatch)
    assert len(live.read_readings(cache_path=path, now=NOW)) == 2


def test_cache_roundtrips_reset_times(tmp_path, monkeypatch):
    reset = NOW + timedelta(days=3)
    monkeypatch.setattr(
        live.quota_telemetry,
        "read_quota_sync",
        lambda engine, **kw: TelemetryReading(engine, "fake", NOW, (WindowReading("codex/10080m", 10, 10080, reset),)),
    )
    path = tmp_path / "cache.json"
    live.read_readings(cache_path=path, now=NOW)
    assert live.read_readings(cache_path=path, now=NOW)["codex"].windows[0].resets_at == reset
