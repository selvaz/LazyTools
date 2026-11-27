"""The one place that decides who reviews a ``--review-of`` job.

Every caller asks :func:`choose_reviewer`: local routing (``route``/``run --tier``),
the cloud review path, and the job launcher (whose native-id check is the last row).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from lazybridge.policy import PolicyError, select_reviewer

ENGINES = ("codex", "claude")


@dataclass(frozen=True)
class ReviewerChoice:
    """``engine``/``independence`` when a reviewer is allowed, else ``refusal_reason``."""

    engine: str | None
    independence: str | None
    refusal_reason: str | None = None


def opposite_engine(engine: str) -> str:
    """The other bridge engine; ``ValueError`` for an unknown one."""
    if engine not in ENGINES:
        raise ValueError(f"no opposite engine for {engine!r}")
    return ENGINES[1 - ENGINES.index(engine)]


def _refused(reason: str) -> ReviewerChoice:
    return ReviewerChoice(None, None, reason)


def choose_reviewer(
    writer_engine: str,
    installed: Iterable[str],
    explicit_engine: str | None = None,
    session_history: bool = False,
    writer_native_id: str | None = None,
    reviewer_native_id: str | None = None,
) -> ReviewerChoice:
    """Choose the reviewer of ``writer_engine``'s work; the first matching row wins.

    ``installed`` is the discovered engine set (``_installed.installed_engines``);
    ``explicit_engine`` is ``--engine``; ``session_history`` says the reviewer's
    ``--session`` already has a conversation in the repository; the native ids are
    the writer's and the reviewer's conversation ids, when known.

    ====  ==========================================  =========================================
    row   condition                                   outcome
    ====  ==========================================  =========================================
    1     reviewer native id == writer native id      refused: not independent
    2     opposite engine installed, or explicit      opposite engine, ``opposite-engine``
          ``--engine`` names it
    3     writer's engine is the only one installed   writer's engine, ``same-provider-fresh``
    4     neither 2 nor 3                             refused: opposite engine unavailable
    5     2/3 chose, but ``--engine`` names another   refused: runs on the chosen engine
    6     3 chose and ``session_history``             refused: needs a new session name
    ====  ==========================================  =========================================

    An explicit opposite engine asserts it is available (governed reviews pass
    ``--engine``); discovery only decides automatic selection. The native-id
    check needs both ids. A launcher whose engine is already fixed passes
    ``installed={engine}`` and ``explicit_engine=engine``: rows 2-6 then never
    refuse and only row 1 can.
    """
    if reviewer_native_id and reviewer_native_id == writer_native_id:
        return _refused(
            f"review refused as not independent: native conversation id {reviewer_native_id} is the one used by "
            "the writer job; use a new --session alias"
        )
    opposite = opposite_engine(writer_engine)
    available = frozenset(installed) | ({opposite} if explicit_engine == opposite else set())
    try:
        selection = select_reviewer(writer_engine, available, mode="opposite-or-fresh")
    except PolicyError as exc:
        return _refused(str(exc))
    if explicit_engine is not None and explicit_engine != selection.engine:
        return _refused(
            f"an independent review of {writer_engine}'s work runs on {selection.engine}, not {explicit_engine}: "
            f"drop --engine, or pass --engine {selection.engine}"
        )
    if selection.independence == "same-provider-fresh" and session_history:
        return _refused(
            "the --session already has a conversation in this repository; a "
            "same-provider-fresh review needs a new session name or none"
        )
    return ReviewerChoice(selection.engine, selection.independence)


__all__ = ["ENGINES", "ReviewerChoice", "choose_reviewer", "opposite_engine"]
