"""lazytools.projects.cost_report -- project-scoped spend/job visibility."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lazybridge import Store

from lazytools.projects import cost_report
from lazytools.projects.keys import JOB_PREFIX


def _write_job(store: Store, job_id: str, *, plan_id: str, status: str, cost_usd: float, finished_at: datetime | None, kind: str | None = None) -> None:
    store.write(
        f"{JOB_PREFIX}{job_id}",
        {
            "job_id": job_id,
            "plan_id": plan_id,
            "task_index": 0,
            "status": status,
            "cost_usd": cost_usd,
            "created_at": (finished_at or datetime.now(UTC)).isoformat(),
            "finished_at": finished_at.isoformat() if finished_at else None,
            "kind": kind,
        },
    )


def test_project_jobs_filters_by_plan_id() -> None:
    store = Store()
    _write_job(store, "job-a", plan_id="project:alpha", status="done", cost_usd=1.0, finished_at=datetime.now(UTC))
    _write_job(store, "job-b", plan_id="project:beta", status="done", cost_usd=2.0, finished_at=datetime.now(UTC))
    jobs = cost_report.project_jobs(store, "alpha")
    assert [j["job_id"] for j in jobs] == ["job-a"]


def test_project_cost_report_buckets_today_and_seven_days() -> None:
    store = Store()
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    _write_job(store, "job-today", plan_id="project:alpha", status="done", cost_usd=1.5, finished_at=now)
    _write_job(store, "job-3-days-ago", plan_id="project:alpha", status="done", cost_usd=2.5, finished_at=now - timedelta(days=3))
    _write_job(store, "job-10-days-ago", plan_id="project:alpha", status="done", cost_usd=100.0, finished_at=now - timedelta(days=10))
    _write_job(store, "job-other-project", plan_id="project:beta", status="done", cost_usd=999.0, finished_at=now)

    report = cost_report.project_cost_report(store, "alpha", now=now)
    assert report["project_id"] == "alpha"
    assert report["today_usd"] == 1.5
    assert report["last_7_days_usd"] == 4.0  # today + 3 days ago, not 10 days ago or the other project
    assert report["job_count"] == 3


def test_project_cost_report_counts_unmeasured_records() -> None:
    store = Store()
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    _write_job(store, "job-consultant", plan_id="project:alpha", status="done", cost_usd=0.0, finished_at=now, kind="ask_codex")
    report = cost_report.project_cost_report(store, "alpha", now=now)
    assert report["unmeasured_cost_records"]["today"] == 1
    assert report["unmeasured_cost_records"]["last_7_days"] == 1


def test_project_cost_report_ignores_future_timestamps() -> None:
    store = Store()
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    _write_job(store, "job-future", plan_id="project:alpha", status="running", cost_usd=50.0, finished_at=now + timedelta(days=1))
    report = cost_report.project_cost_report(store, "alpha", now=now)
    assert report["today_usd"] == 0.0
    assert report["last_7_days_usd"] == 0.0
    assert report["job_count"] == 1  # still counted in jobs_by_status, just not in spend


def test_project_cost_report_jobs_by_status() -> None:
    store = Store()
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    store.write(
        f"{JOB_PREFIX}job-running",
        {
            "job_id": "job-running",
            "plan_id": "project:alpha",
            "task_index": 0,
            "status": "running",
            "cost_usd": 0.0,
            "created_at": now.isoformat(),  # started before `now`, not finished yet
            "finished_at": None,
        },
    )
    _write_job(store, "job-done", plan_id="project:alpha", status="done", cost_usd=1.0, finished_at=now)
    report = cost_report.project_cost_report(store, "alpha", now=now)
    assert report["jobs_by_status"] == {"running": 1, "done": 1}
    # the running job's created_at IS countable (it is not in the future) -- confirms
    # job_count/status are unconditional while spend buckets stay timestamp-gated.
    assert report["job_count"] == 2
