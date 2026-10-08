from __future__ import annotations

import json
import math
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from lazybridge import Store

from _projects_parity import original_function
from lazytools.projects import cost_report

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
JOB_PREFIX = "custom:job:"
TASK_PREFIX = "custom:turn:"


def original_report():
    dependencies = dict(math=math, json=json, sqlite3=sqlite3, Path=Path, datetime=datetime, UTC=UTC, timedelta=timedelta,
        _UNMEASURED_COST_KINDS=frozenset({"ask_codex", "ask_claude", "ask_designer"}))
    for name in ("_parse_timestamp", "_cost", "_totals", "_read_store_records_read_only", "_sum_totals", "_is_unmeasured", "_unmeasured_cost_counts", "_sum_counts"):
        dependencies[name] = original_function("cost_" + name, **dependencies)
    return original_function("cost_build_fleet_cost_report", **dependencies, TASK_PREFIX=TASK_PREFIX,
        _fresh_store=nullcontext, list_specialists=lambda store: [SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")])


def fixture(store, scale):
    rows = [
        {"cost_usd": scale, "finished_at": NOW.isoformat(), "plan_id": "project:one"},
        {"cost_usd": 2 * scale, "created_at": (NOW - timedelta(days=2)).isoformat(), "plan_id": "project:two"},
        {"cost_usd": 3 * scale, "created_at": (NOW - timedelta(days=8)).isoformat()},
        {"cost_usd": 100, "created_at": (NOW + timedelta(days=1)).isoformat()},
        {"cost_usd": 0, "kind": "ask_codex", "created_at": NOW.isoformat()},
        {"cost_usd": 0, "cost_unknown": True, "created_at": (NOW - timedelta(days=1)).isoformat()},
        {"cost_usd": "bad", "finished_at": "bad", "created_at": NOW.isoformat()},
        {"cost_usd": True, "created_at": NOW.isoformat()},
    ]
    for index, row in enumerate(rows):
        store.write(f"{JOB_PREFIX}{index}", row)
    store.write(f"{TASK_PREFIX}today", {"cost_usd": 4 * scale, "completed_at": NOW.isoformat()})
    store.write(f"{TASK_PREFIX}old", {"cost_usd": 5 * scale, "completed_at": (NOW - timedelta(days=3)).isoformat()})
    store.write("unrelated:job", {"cost_usd": 1000, "created_at": NOW.isoformat()})
    store.write(f"{JOB_PREFIX}not-a-record", "legacy")


def test_fleet_report_matches_original_numbers_over_all_projects(tmp_path):
    primary = Store()
    fixture(primary, 1)
    paths = {name: tmp_path / f"{name}.sqlite" for name in ("alpha", "beta")}
    for scale, path in enumerate(paths.values(), start=2):
        store = Store(str(path))
        fixture(store, scale)
        store.close()
    snapshots = {name: path.read_bytes() for name, path in paths.items()}
    before = original_report()(primary, job_prefix=JOB_PREFIX, specialist_store_path=paths.__getitem__, now=NOW)
    after = cost_report.build_fleet_cost_report(primary, specialist_stores=paths, job_prefix=JOB_PREFIX, task_prefix=TASK_PREFIX, now=NOW)
    assert after == before
    assert after["today_usd"] == 30 and after["last_7_days_usd"] == 72
    assert after["unmeasured_cost_records"] == {"today": 3, "last_7_days": 6}
    assert {name: path.read_bytes() for name, path in paths.items()} == snapshots


def test_fleet_store_mapping_without_turns_or_project_filter():
    primary, specialist = Store(), Store()
    fixture(primary, 1)
    fixture(specialist, 2)
    result = cost_report.build_fleet_cost_report(primary, specialist_stores={"alpha": specialist}, job_prefix=JOB_PREFIX, now=NOW)
    assert result["today_usd"] == 3 and result["last_7_days_usd"] == 9
    assert result["unavailable"] == []


@pytest.mark.parametrize("kind", ["missing", "schema", "json"])
def test_unavailable_specialist_is_reported_without_partial_cost(tmp_path, kind):
    path = tmp_path / "bad.sqlite"
    if kind != "missing":
        with sqlite3.connect(path) as connection:
            if kind == "json":
                connection.execute("CREATE TABLE store(key TEXT, value TEXT)")
                connection.execute("INSERT INTO store VALUES (?, ?)", (f"{JOB_PREFIX}one", json.dumps({"cost_usd": 100, "created_at": NOW.isoformat()})))
                connection.execute("INSERT INTO store VALUES (?, ?)", (f"{TASK_PREFIX}bad", "invalid-json"))
    result = cost_report.build_fleet_cost_report(Store(), specialist_stores={"bad": path}, job_prefix=JOB_PREFIX, task_prefix=TASK_PREFIX, now=NOW)
    assert result["today_usd"] == 0 and result["last_7_days_usd"] == 0
    assert result["unavailable"][0]["name"] == "bad"
    assert next(row for row in result["breakdown"] if row["name"] == "bad")["available"] is False
    assert path.exists() == (kind != "missing")


def test_fleet_prefixes_cannot_double_count_same_family():
    with pytest.raises(ValueError, match="double counting"):
        cost_report.build_fleet_cost_report(Store(), specialist_stores={}, task_prefix=JOB_PREFIX, job_prefix=JOB_PREFIX)
