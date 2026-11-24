"""Sign, persist and verify bridge publication receipts."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from lazytools.code_bridge import _cloud, _gh, _store
from lazytools.projects.task_service import AcceptedRun
from lazytools.projects.task_store import TaskServiceError, publication_receipt, save_publication_receipt, transaction

if TYPE_CHECKING:
    from lazybridge.ext.receipt import IssuerKey, PublicationReceipt

    from lazytools.code_bridge._publication import PublicationAttempt, PublicationOutcome

_SAFE_CHECK = re.compile(
    r"(?:(?:python(?:3)? -m )?(?:pytest|ruff|mypy)|npm run (?:build|lint)|npm test|git diff --check)"
    r"(?: (?:-q|-v|check|format|--check))*"
)
_SAFE_ID = re.compile(r"[a-zA-Z0-9_./@:-]+")
_SAFE_JOB = re.compile(r"[a-zA-Z0-9_-]+")


def issuer_path() -> Path:
    configured = os.environ.get("LAZYTOOLS_RECEIPT_ISSUER_KEY")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".lazytools" / "receipt-issuer" / "ed25519.pem"


def public_key_path() -> Path:
    return issuer_path().with_suffix(".pub.pem")


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        candidate = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.close()
            candidate.replace(path)
        finally:
            candidate.unlink(missing_ok=True)


def load_issuer() -> IssuerKey:
    from lazybridge.ext.receipt import IssuerKey

    key = IssuerKey(issuer_path())
    _write_atomic(public_key_path(), key.public_key_pem())
    return key


def _identifier(value: object) -> str:
    if isinstance(value, str) and _SAFE_ID.fullmatch(value) and not re.match(r"(?:[a-zA-Z]:)?/", value):
        return value
    return ""


def _run_evidence(run: AcceptedRun | None, job_store: Any, fallback_task: str = "") -> dict[str, Any]:
    if run is None:
        meta = {}
    else:
        meta = _store.read_meta(job_store, run.job_ref) or {}
    mode = meta.get("launch_mode")
    eligible = mode == "governed" and bool(meta.get("release_receipt"))
    reasons = meta.get("policy_ineligibility") or ([] if eligible else ["Admission was not recorded"])
    if mode not in {"governed", "ungoverned-legacy"}:
        mode = "unknown"
    # Core compiler diagnostics are generated locally. Never export arbitrary
    # provider messages, paths, prompts or command output.
    safe_reasons = [
        _cloud.redact(reason) if isinstance(reason, str) and re.fullmatch(r"[a-zA-Z0-9 .'-]+", reason) else "Redacted"
        for reason in reasons
    ]
    return {
        "task_id": run.task_id if run else fallback_task,
        "attempt": run.attempt if run else 0,
        "engine": run.evidence.engine_family if run else "",
        "model": _identifier(meta.get("model")),
        "launch_mode": mode,
        "admission": {"eligible": eligible, "reasons": safe_reasons},
        "policy": {
            "profile": _identifier(meta.get("profile")),
            "revision": _identifier(meta.get("policy_revision")),
            "digest": _identifier(meta.get("policy_digest")),
            "folders": ["<repository>"] if meta.get("cwd") else [],
            "network": _identifier(meta.get("network")),
            "tool_classes": [value for value in map(_identifier, meta.get("policy_tool_classes", [])) if value],
        },
    }


def template(attempt: PublicationAttempt, job_store: Any, key: IssuerKey) -> PublicationReceipt:
    from lazybridge.ext.receipt import PublicationReceipt

    review = attempt.review
    writer = _run_evidence(review.writer, job_store, attempt.task_id)
    reviewer = _run_evidence(review.reviewer, job_store)
    owner, name = attempt.repository.split("/")
    writer_meta = _store.read_meta(job_store, review.writer.job_ref) if review.writer else None
    reviewer_meta = _store.read_meta(job_store, review.reviewer.job_ref) if review.reviewer else None
    parent = _identifier((writer_meta or {}).get("parent_policy_digest"))
    if parent != _identifier((reviewer_meta or {}).get("parent_policy_digest")):
        parent = ""
    return PublicationReceipt.model_validate(
        {
            "receipt_id": str(uuid4()),
            "issued_at": datetime.now(UTC).isoformat(),
            "issuer": {"key_id": key.key_id, "public_key_fingerprint": key.fingerprint()},
            "repository": {"owner": owner, "name": name, "base_branch": attempt.base_branch},
            "publication": {
                "head_branch": attempt.head_branch,
                "head_sha": attempt.head_sha,
                "base_sha": "",
                "pr_number": None,
                "pr_url": None,
            },
            "writer": writer,
            "reviewer": {
                **reviewer,
                # Version 1 requires a value even without an accepted independent
                # review. Only a verdict of accepted attests this mode.
                "independence_mode": review.independence or "opposite-engine",
                "reviewed_sha": attempt.head_sha,
                "verdict": "accepted" if review.independence else "unavailable",
            },
            "delegation": {"parent_digest": parent},
            "approval": {
                "ticket_id": None,
                "decided_by": None,
                "decided_at": None,
                "consumed_at": None,
                "request_digest": None,
                "target": {
                    "repository": attempt.repository,
                    "head_branch": attempt.head_branch,
                    "head_sha": attempt.head_sha,
                },
            },
            "checks": [
                {
                    "name": f"check-{index}",
                    "command": check.command if _SAFE_CHECK.fullmatch(check.command) else "[redacted]",
                    "status": check.outcome,
                    "head_sha": attempt.head_sha,
                }
                for index, check in enumerate(review.checks, 1)
                if (check.model_extra or {}).get("head_sha", attempt.head_sha) == attempt.head_sha
            ],
            "outcome": {"status": "refused", "refusal_reason": "publication_not_attempted"},
        }
    )


def finish(
    receipt: PublicationReceipt,
    result: PublicationOutcome,
    key: IssuerKey,
    *,
    projects_db: Path | None,
    job_db: Path | None,
    job_ref: str | None,
) -> PublicationReceipt:
    from lazybridge.ext.receipt import PublicationReceipt, sign, verify

    if result.stored_receipt is not None:
        stored = PublicationReceipt.model_validate_json(result.stored_receipt)
        verification = verify(stored, key.public_key_pem())
        if not verification.valid:
            raise TaskServiceError("invalid_receipt", f"Stored receipt verification failed: {verification.reason}")
        write_evidence(stored.receipt_id, result.stored_receipt, job_db, job_ref)
        return stored
    ticket = result.ticket
    bound = ticket if result.reason != "approval_target_mismatch" else None
    decision = bound.decision if bound else None
    fields = receipt.model_dump(mode="json")
    fields["publication"].update(base_sha=result.base_sha, pr_number=result.pr_number, pr_url=result.pr_url)
    fields["approval"].update(
        ticket_id=ticket.approval_id if ticket else None,
        decided_by=_identifier(decision.decider) if decision else None,
        decided_at=decision.decided_at.isoformat() if decision else None,
        consumed_at=bound.consumed_at.isoformat() if bound and bound.consumed_at else None,
        request_digest=bound.request.digest if bound and bound.request else None,
    )
    status: Literal["refused", "published"] = "refused" if result.reason else "published"
    fields["outcome"] = {"status": status, "refusal_reason": result.reason}
    receipt = sign(PublicationReceipt.model_validate(fields), key)
    encoded = json.dumps(receipt.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    save_publication_receipt(
        projects_db,
        receipt_id=receipt.receipt_id,
        task_id=receipt.writer.task_id,
        attempt=receipt.writer.attempt,
        head_sha=receipt.publication.head_sha,
        pr_number=receipt.publication.pr_number,
        status=status,
        receipt_json=encoded,
        created_at=receipt.issued_at,
        consumed_ticket_id=ticket.approval_id if result.consumed and ticket else None,
    )
    write_evidence(receipt.receipt_id, encoded, job_db, job_ref)
    return receipt


def write_evidence(receipt_id: str, encoded: str, job_db: Path | None, job_ref: str | None) -> None:
    evidence_dir = _store.results_dir(job_db)
    if job_ref and _SAFE_JOB.fullmatch(job_ref):
        evidence_dir /= job_ref
    path = evidence_dir / "publication" / receipt_id / "receipt.json"
    content = encoded.encode("utf-8")
    try:
        if path.read_bytes() == content:
            return
    except FileNotFoundError:
        pass
    _write_atomic(path, content)


def stored_json(projects_db: Path | None, receipt_id: str) -> str | None:
    with transaction(projects_db) as store:
        return publication_receipt(store, receipt_id)


def read_source(projects_db: Path | None, source: str) -> str:
    try:
        identifier = str(UUID(source))
    except ValueError:
        return Path(source).read_text(encoding="utf-8")
    raw = stored_json(projects_db, identifier)
    if raw is None:
        raise TaskServiceError("not_found", "Publication receipt not found")
    return raw


def verify_json(raw: str, *, live: bool = False, public_key: Path | None = None) -> dict[str, Any]:
    from lazybridge.ext.receipt import PublicationReceipt, verify

    try:
        receipt = PublicationReceipt.model_validate_json(raw)
    except ValueError:
        return {"valid": False, "reason": "invalid_receipt"}
    try:
        pem = (public_key or public_key_path()).read_bytes()
    except OSError:
        return {"valid": False, "reason": "public_key_unavailable"}
    result = verify(receipt, pem)
    if result.valid and live:
        number = receipt.publication.pr_number
        if number is None:
            return {"valid": False, "reason": "no_pull_request"}
        repo = f"github.com/{receipt.repository.owner}/{receipt.repository.name}"
        try:
            head = _gh.pr_view(repo, number).get("headRefOid")
        except (_cloud.CloudError, OSError, subprocess.SubprocessError):
            return {"valid": False, "reason": "live_head_unavailable"}
        if not isinstance(head, str) or not head:
            return {"valid": False, "reason": "live_head_unavailable"}
        result = verify(receipt, pem, expected_head_sha=head)
    return {"valid": result.valid, "reason": result.reason}
