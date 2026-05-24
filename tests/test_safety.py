"""Safety primitives: allow-list + one-shot, scope-bound confirmation grants."""

from __future__ import annotations

from lazytools.safety import ActionBlocked, Allowlist, ConfirmationGate

# --- Allowlist --------------------------------------------------------- #


def test_none_allows_everything() -> None:
    al = Allowlist(None)
    assert al.permits("anyone@x.com") is True
    assert al.permits(12345) is True


def test_empty_denies_everything() -> None:
    al = Allowlist([])
    assert al.permits("anyone@x.com") is False


def test_allowlist_is_case_insensitive() -> None:
    al = Allowlist(["OK@X.com"])
    assert al.permits("ok@x.com") is True
    assert al.permits("OK@X.COM") is True
    assert al.permits("nope@x.com") is False


def test_allowlist_normalizes_non_strings() -> None:
    al = Allowlist([42, "abc"])
    assert al.permits(42) is True
    assert al.permits("42") is True
    assert al.permits(99) is False


# --- ConfirmationGate -------------------------------------------------- #


def test_disabled_gate_always_permits_without_grants() -> None:
    gate = ConfirmationGate(enabled=False)
    assert gate.enabled is False
    assert gate.consume("a@x.com") is True
    assert gate.consume("a@x.com") is True  # not consumed — always true


def test_grant_authorizes_exactly_one_action() -> None:
    gate = ConfirmationGate()
    gate.grant("a@x.com")
    assert gate.consume("a@x.com") is True
    assert gate.consume("a@x.com") is False  # one-shot: spent


def test_grant_is_target_bound() -> None:
    gate = ConfirmationGate()
    gate.grant("alice@x.com")
    assert gate.consume("bob@x.com") is False
    assert gate.consume("alice@x.com") is True


def test_grant_any_authorizes_one_send_to_any_target() -> None:
    gate = ConfirmationGate()
    gate.grant_any()
    assert gate.consume("whoever@x.com") is True
    assert gate.consume("whoever@x.com") is False


def test_target_grant_preferred_over_any_grant() -> None:
    gate = ConfirmationGate()
    gate.grant_any()
    gate.grant("a@x.com")
    # The tighter target-bound grant is spent first, leaving the any-grant.
    assert gate.consume("a@x.com") is True
    assert gate.consume("b@x.com") is True  # the surviving any-grant
    assert gate.consume("c@x.com") is False


def test_no_sticky_global_approval() -> None:
    # Two independent gates do not share grants — no process-global state.
    g1, g2 = ConfirmationGate(), ConfirmationGate()
    g1.grant_any()
    assert g2.consume("a@x.com") is False


# --- Scope binding ----------------------------------------------------- #


def test_scope_bound_grant_needs_matching_scope() -> None:
    gate = ConfirmationGate()
    gate.grant("a@x.com", scope="TASK-A")
    assert gate.consume("a@x.com", scope="TASK-B") is False
    assert gate.consume("a@x.com", scope=None) is False
    assert gate.consume("a@x.com", scope="TASK-A") is True


def test_unscoped_grant_consumable_in_any_scope() -> None:
    gate = ConfirmationGate()
    gate.grant("a@x.com")
    assert gate.consume("a@x.com", scope="TASK-X") is True


def test_scope_specific_grant_preferred_over_unscoped() -> None:
    gate = ConfirmationGate()
    gate.grant("a@x.com")  # unscoped
    gate.grant("a@x.com", scope="TASK-A")  # scoped
    # In TASK-A the scoped grant is consumed first; the unscoped one survives.
    assert gate.consume("a@x.com", scope="TASK-A") is True
    assert gate.consume("a@x.com", scope="TASK-A") is True  # unscoped survivor
    assert gate.consume("a@x.com", scope="TASK-A") is False


def test_action_blocked_is_permission_error() -> None:
    assert issubclass(ActionBlocked, PermissionError)
