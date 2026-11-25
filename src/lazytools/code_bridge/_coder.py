"""Provision the owner's coder hand and narrow it for one writer job."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from lazybridge import Store
from lazybridge.ext.approval import ApprovalQueue
from lazybridge.policy import (
    ApprovalCard,
    ApprovalTier,
    CardMatcher,
    EffectivePolicy,
    PolicyWidening,
    TaskGrant,
    policy_scope,
    resolve,
)

from lazytools.code_bridge import _coder_actions, _credentials
from lazytools.projects.task_store import transaction

ACTION = "code_bridge_local"
TIERS: dict[str, ApprovalTier] = {ACTION: "routine"}
_HAND_KEY = "code-bridge:coder-hand"


def provision(path: Path) -> bool:
    credential = _credentials.load(path)
    with transaction(path, write=True) as store:
        if store.read(_HAND_KEY) is not None:
            return False
        queue = ApprovalQueue(cast(Store, store), action_tiers=TIERS)
        cards = tuple(
            queue.issue_card(
                credential=credential, action_class=ACTION, tier="routine",
                matcher=CardMatcher(command=command), transferable=True,
            )
            for command in _coder_actions.COMMANDS
        )
        queue.set_card_template("coder", tuple(card.card_id for card in cards), credential=credential)
        store.write(_HAND_KEY, [card.model_dump(mode="json") for card in cards])
        return True


def _policy(folder: Path, cards: tuple[ApprovalCard, ...]) -> EffectivePolicy:
    # The card adapter describes shell or file effects. Native policy is
    # checked by BridgeGate before any request reaches this adapter.
    return resolve("edit-and-run", TaskGrant(
        folder, {"tool_classes": ["shell"]}, "off", "local", approval_cards=cards,
    ))


@dataclass(frozen=True)
class Hand:
    workspace: _coder_actions.Workspace
    policies: dict[Path, EffectivePolicy]


def copy_for_job(
    store: Store, queue: ApprovalQueue, cwd: Path, temp: Path | None, task_id: str, expires: datetime,
    record: Callable[[str], None],
) -> Hand | None:
    originals = tuple(ApprovalCard.model_validate(raw) for raw in store.read(_HAND_KEY, []))
    if not originals:
        return None
    workspace = _coder_actions.Workspace.capture(cwd, temp)
    policies = {}
    # The 1.10 template-copy helper preserves the original matcher. Copy
    # individually to bind the path, task, expiry and nontransferability.
    with policy_scope(_policy(cwd, originals)) as holder:
        assert holder is not None
        for root in workspace.roots:
            copies = []
            for original in originals:
                try:
                    copy = queue.copy_card(
                        original.card_id, holder=holder,
                        matcher=original.matcher.model_copy(update={"path_prefix": root}),
                        task_id=task_id, expires_at=expires,
                    )
                except PolicyWidening:
                    # The owner may have revoked an original during the job launch.
                    continue
                record(copy.card_id)
                copies.append(copy)
            policies[root] = _policy(root, tuple(copies))
    return Hand(workspace, policies)
