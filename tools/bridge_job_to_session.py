"""Export a finished code-bridge job for LazyBridge viz replay.

Example: python tools/bridge_job_to_session.py JOB_ID --db jobs.sqlite --output replay.sqlite
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from lazybridge import Session
from lazybridge.ext.viz._normalizer import normalise_event
from lazybridge.session import EventType, redact_secrets

from lazytools.code_bridge import _jobs, _registry, _store


def timestamp(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return 0.0


def export_job(job_id: str, db: Path, output: Path) -> tuple[str, dict[str, int]]:
    if not db.is_file():
        raise ValueError(f"Job database does not exist: {db}")
    if db.resolve() == output.resolve():
        raise ValueError("Output must be a different database from the job database")
    store = _store.build_store(db)
    registry = None
    try:
        job = _jobs.find_job(store, _store.build_job_registry(store), job_id)
        if job is None:
            raise ValueError(f"Job not found or ambiguous: {job_id}")
        if job.get("status") not in _store.TERMINAL_STATUSES:
            raise ValueError(f"Job is not finished: {job_id}")
        job_id = job["job_id"]
        registry = _registry.Registry.for_job(store, job_id, _registry.isolated_path(db, None))
        tickets = [registry.inspect(ticket_id) for ticket_id in job.get("registry_ticket_ids", [])]
        if any(ticket is None for ticket in tickets):
            raise ValueError("A recorded approval ticket could not be found in its registry")
        tickets += [
            registry.legacy.get_ticket(key.removeprefix(_store.APPROVAL_PREFIX))
            for key, raw in store.items(prefix=_store.APPROVAL_PREFIX)
            if isinstance(raw, dict) and raw.get("task_id") == job_id
        ]
        agent = f"code-bridge-{job.get('kind') or job.get('engine') or 'job'}"
        events = [(timestamp(job.get("created_at")), EventType.AGENT_START,
                   {"agent_name": agent, "task": job.get("objective"), "job_id": job_id})]
        for turn in job.get("turns", []):
            events.append((timestamp(turn.get("started_at")), EventType.LOOP_STEP,
                           {"agent_name": agent, **turn}))
        for ticket in tickets:
            if ticket is not None:
                events.append((ticket.created_at.timestamp(), EventType.HIL_DECISION,
                               {"agent_name": agent, "kind": "approval", **ticket.model_dump(mode="json")}))
        finished = job.get("completed_at") or job.get("updated_at") or 0
        events.append((timestamp(finished), EventType.AGENT_FINISH,
                       {"agent_name": agent, "result": job.get("result"), "error": job.get("error"),
                        "status": job["status"]}))
        rejected: dict[str, int] = {}
        with Session(db=str(output)) as session:
            rows = []
            for ts, event_type, payload in sorted(events, key=lambda event: event[0]):
                try:
                    normalized = normalise_event(redact_secrets(payload))
                    encoded = json.dumps(normalized)
                except (TypeError, ValueError, OverflowError):
                    rejected[str(event_type)] = rejected.get(str(event_type), 0) + 1
                    continue
                rows.append((session.session_id, job_id, str(event_type), encoded, ts))
            session.events.record_many(rows)
            return session.session_id, rejected
    finally:
        if registry is not None and registry._store is not None:
            registry._store.close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        session_id, rejected = export_job(args.job_id, args.db, args.output)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Session: {session_id} ({args.output})")
    print(f"Rejected event types: {json.dumps(rejected, sort_keys=True)}")


if __name__ == "__main__":
    main()
