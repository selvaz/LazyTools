"""Provision the owner's coder hand and narrow it for one writer job."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from lazybridge import Store
from lazybridge.engines.coding import ApprovalRequest
from lazybridge.ext.approval import ApprovalQueue
from lazybridge.policy import (
    ApprovalCard,
    CardMatcher,
    CardTier,
    EffectivePolicy,
    PolicyWidening,
    TaskGrant,
    policy_scope,
    resolve,
)

from lazytools.code_bridge import _coder_actions, _credentials
from lazytools.projects.task_store import transaction

ACTION = "code_bridge_local"
MODES = ("supervised", "total")
DEFAULT_PRESET = "with-production"
TIERS: dict[str, CardTier] = {
    ACTION: "routine",
    **dict.fromkeys(("git_local", "git_push_job_branch", "gh_pr_create", "checks_in_checkout"), "routine"),
    **dict.fromkeys(_coder_actions.TICKET_COMMANDS.keys() - _coder_actions.OWNER_CLASSES
                    - {"git_local", "git_push_job_branch", "gh_pr_create", "checks_in_checkout"}, "sensitive"),
}
_HAND_KEY = "code-bridge:coder-hand"


def _presets_text() -> str:
    return files(__package__).joinpath("autonomy_presets.toml").read_text(encoding="utf-8")


def definitions_digest() -> str:
    """Digest of the definitions the coder cards are issued from.

    A persisted hand whose digest differs was issued from older ticket classes,
    presets or command rules and must be re-issued.
    """
    definitions = {
        "commands": {kind: [list(command) for command in commands]
                     for kind, commands in _coder_actions.TICKET_COMMANDS.items()},
        "network": sorted(_coder_actions.NETWORK_CLASSES),
        "owner": sorted(_coder_actions.OWNER_CLASSES),
        "tiers": TIERS,
        "presets": tomllib.loads(_presets_text()),
    }
    return hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest()


def delegated_classes(mode: str, preset: str) -> frozenset[str]:
    if mode not in MODES:
        raise ValueError(f"Unknown autonomy mode: {mode}")
    presets = tomllib.loads(_presets_text())
    if preset not in presets:
        raise ValueError(f"Unknown autonomy preset: {preset}")
    settings = presets[preset]
    known = set(_coder_actions.TICKET_COMMANDS)
    floor = frozenset(settings["floor"])
    supervised = frozenset(settings["supervised"])
    if not _coder_actions.OWNER_CLASSES <= floor <= known or not supervised <= known - floor:
        raise ValueError(f"Invalid autonomy preset: {preset}")
    return supervised if mode == "supervised" else frozenset(known - floor)


@dataclass(frozen=True)
class Selection:
    mode: str = "supervised"
    preset: str = DEFAULT_PRESET
    cards: tuple[ApprovalCard, ...] = ()

    @classmethod
    def from_record(cls, raw: Any) -> Selection:
        if not isinstance(raw, dict):
            return cls(cards=tuple(ApprovalCard.model_validate(card) for card in raw or []))
        return cls(
            mode=raw["mode"], preset=raw["preset"],
            cards=tuple(ApprovalCard.model_validate(card) for card in raw["cards"]),
        )


def configure(path: Path, mode: str, preset: str | None) -> Selection:
    """Replace the registry's coder hand in the same transaction as its card changes."""
    credential = _credentials.load(path)
    _credentials.require_outside_checkout(path)
    with transaction(path, write=True) as store:
        queue = ApprovalQueue(cast(Store, store), action_tiers=TIERS)
        queue._require_owner(credential)
        raw = store.read(_HAND_KEY)
        previous = Selection.from_record(raw)
        preset = preset or previous.preset
        classes = delegated_classes(mode, preset)
        digest = definitions_digest()
        if isinstance(raw, dict) and (mode, preset, digest) == (previous.mode, previous.preset, raw.get("digest")):
            return previous
        cards = tuple(
            queue.issue_card(
                credential=credential, action_class=kind, tier=TIERS[kind], transferable=True,
                matcher=CardMatcher(command=command, network="on" if kind in _coder_actions.NETWORK_CLASSES else "off"),
            )
            for kind in sorted(classes)
            for command in _coder_actions.TICKET_COMMANDS[kind]
        )
        for card in previous.cards:
            queue.revoke_card(card.card_id, credential=credential)
        queue.set_card_template("coder", tuple(card.card_id for card in cards), credential=credential)
        store.write(_HAND_KEY, {
            "mode": mode, "preset": preset, "digest": digest,
            "cards": [card.model_dump(mode="json") for card in cards],
        })
        return Selection(mode, preset, cards)


def show(path: Path) -> Selection:
    if not path.is_file():
        return Selection()
    with transaction(path) as store:
        return Selection.from_record(store.read(_HAND_KEY))


def provision(path: Path) -> bool:
    credential = _credentials.load(path)
    with transaction(path, write=True) as store:
        if store.read(_HAND_KEY) is not None:
            return False
        queue = ApprovalQueue(cast(Store, store), action_tiers=TIERS)
        cards = tuple(
            queue.issue_card(
                credential=credential, action_class=ACTION, tier="routine",
                matcher=CardMatcher(command=command, network="on" if command[0] == "gh" else "off"), transferable=True,
            )
            for command in _coder_actions.COMMANDS
        )
        queue.set_card_template("coder", tuple(card.card_id for card in cards), credential=credential)
        store.write(_HAND_KEY, [card.model_dump(mode="json") for card in cards])
        return True


def _policy(folder: Path, cards: tuple[ApprovalCard, ...]) -> EffectivePolicy:
    # The card adapter describes shell or file effects. Native policy is
    # checked by BridgeGate before any request reaches this adapter.
    network = "on" if any(card.matcher.network == "on" for card in cards) else "off"
    return resolve("edit-and-run", TaskGrant(
        folder, {"tool_classes": ["shell"]}, network, "local", approval_cards=cards,
    ))


@dataclass(frozen=True)
class Hand:
    workspace: _coder_actions.Workspace
    policies: dict[Path, EffectivePolicy]
    selection: Selection | None
    network: str

    def admits(self, request: ApprovalRequest, kind: str) -> bool:
        """Whether the job's own authority and copied cards could approve this request.

        Checked before any workspace probe, since some probes (git push --dry-run,
        gh repo set-default --view) reach the network themselves.
        """
        action_class = kind if self.selection else ACTION
        # The same ticket kind Workspace.action receives, so both agree on the network state.
        network = self.workspace.network(request, kind if self.selection else None)
        if network == "on" and self.network != "on":
            return False
        now = datetime.now(UTC)
        return any(
            card.action_class == action_class and card.matcher.network == network
            and (card.expires_at is None or card.expires_at > now)
            and (network == "off" or policy.network == "on")
            for policy in self.policies.values() for card in policy.approval_cards
        )


def copy_for_job(
    store: Store, queue: ApprovalQueue, cwd: Path, temp: Path | None, task_id: str, expires: datetime,
    record: Callable[[str], None], *, network: str,
) -> Hand | None:
    raw = store.read(_HAND_KEY)
    selection = Selection.from_record(raw)
    originals = tuple(card for card in selection.cards if card.matcher.network == "off" or network == "on")
    if not originals:
        return None
    configured = isinstance(raw, dict)
    workspace = _coder_actions.Workspace.capture(cwd, temp, autonomy=configured)
    policies = {}
    # The 1.10 template-copy helper preserves the original matcher. Copy
    # individually to bind the path, task, expiry and nontransferability.
    with policy_scope(_policy(cwd, originals)) as holder:
        assert holder is not None
        for root in workspace.roots:
            copies = []
            for original in originals:
                if original.action_class == "git_push_job_branch" and (root != cwd or workspace.branch is None):
                    continue
                restrictions: dict[str, Any] = {"path_prefix": root}
                if original.action_class == "git_push_job_branch":
                    restrictions["branch"] = workspace.branch
                try:
                    copy = queue.copy_card(
                        original.card_id, holder=holder,
                        matcher=original.matcher.model_copy(update=restrictions),
                        task_id=task_id, expires_at=expires,
                    )
                except PolicyWidening:
                    # The owner may have revoked an original during the job launch.
                    continue
                record(copy.card_id)
                copies.append(copy)
            policies[root] = _policy(root, tuple(copies))
    return Hand(workspace, policies, selection if configured else None, network)
