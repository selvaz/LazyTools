"""Checking a total against the parts it is supposed to be made of.

Every silent error this codebase has hit had the same shape: a number that
resolved cleanly, carried full provenance, and was not what it claimed to be.
Provenance does not protect against that. Arithmetic does — not by proving a
figure right, which no arithmetic can do from one fact, but by refusing to let
an aggregate pass unchecked when the filing also discloses its parts.

The four outcomes that matter are different findings and must not collapse into
"it didn't add up":

* **balanced** — the total equals its parts. The strongest statement available.
* **residual** — it does not, and the difference is named. Common and fine: a
  debt total legitimately differs from its components by issuance costs or
  premiums, once someone says so.
* **scope conflict** — the total equals ONE component while other non-zero
  parts exist. This is the Cisco shape: ``AmortizationOfIntangibleAssets`` at
  $698m looks like a total and is the operating-expense slice, with $955m more
  in cost of sales. Nothing about the number itself gives it away.
* **unreconciled** — it does not add up and nobody has said why. Not an error to
  swallow; a fact about the filing to report.

Tolerance here is for **rounding, not for economics**. A statement rendered in
millions cannot resolve below a million, so summing five components can drift by
a few million with nothing wrong. A tolerance wide enough to absorb a real
discrepancy is not a tolerance, it is a way of not noticing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReconciliationStatus = Literal[
    "balanced", "residual", "scope_conflict", "unreconciled", "incomplete", "no_total"
]


@dataclass(frozen=True)
class Component:
    """One named part of a total, or the fact that it could not be read."""

    name: str
    value: float | None


@dataclass(frozen=True)
class Reconciliation:
    """What a total and its parts say about each other."""

    name: str
    total: float | None
    components: tuple[Component, ...]
    residual: float | None
    tolerance: float
    status: ReconciliationStatus
    detail: str

    @property
    def ok(self) -> bool:
        """The total is confirmed by its parts, exactly or with a named residual."""
        return self.status in ("balanced", "residual")

    @property
    def blocking(self) -> bool:
        """Anything built on this total would be built on an unchecked number."""
        return self.status in ("scope_conflict", "unreconciled", "incomplete")

    @property
    def known(self) -> tuple[Component, ...]:
        return tuple(c for c in self.components if c.value is not None)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if c.value is None)


def reconcile(
    name: str,
    total: float | None,
    components: dict[str, float | None] | list[Component],
    *,
    rounding_unit: float = 0.0,
    residual_label: str | None = None,
    residual: float | None = None,
) -> Reconciliation:
    """Test ``total`` against ``components``, which must be mutually exclusive.

    Args:
        name: what is being reconciled, for the message.
        total: the reported total, or ``None`` when the filing states none.
        components: the disjoint parts. A ``None`` value means a part exists but
            could not be read — which is *not* the same as it being zero, and is
            why such a case comes back ``incomplete`` rather than unreconciled.
        rounding_unit: the smallest amount the source can express — 1_000_000
            for a statement rendered in millions. Tolerance is that unit times
            the number of known parts, because each one can be rounded once.
            Leave at 0 for exact sources such as XBRL facts.
        residual_label: names a difference that is expected, e.g. "unamortised
            issuance costs". Supplying it turns an unexplained gap into a
            reported one; it does not make the gap go away.
        residual: the expected difference's amount, when it is itself disclosed.
            The gap must match it, or the result is ``unreconciled`` — a named
            residual of the wrong size explains nothing.

    Components are assumed **mutually exclusive**. Passing a total alongside its
    own sub-total double counts, and no check here can detect that: it is the
    caller's job to choose parts that do not overlap.
    """
    parts = (
        [Component(k, v) for k, v in components.items()]
        if isinstance(components, dict)
        else list(components)
    )
    known = [c for c in parts if c.value is not None]
    missing = [c.name for c in parts if c.value is None]
    tolerance = abs(rounding_unit) * max(len(known), 1)

    if total is None:
        return Reconciliation(
            name, None, tuple(parts), None, tolerance, "no_total",
            f"{name}: no reported total to check the parts against",
        )
    if missing:
        return Reconciliation(
            name, total, tuple(parts), None, tolerance, "incomplete",
            f"{name}: {len(missing)} part(s) unreadable ({', '.join(missing)}), so the "
            "total cannot be confirmed — an unread part is not a zero one",
        )
    if not known:
        return Reconciliation(
            name, total, tuple(parts), None, tolerance, "incomplete",
            f"{name}: no parts to check the total against",
        )

    subtotal = sum(c.value or 0.0 for c in known)
    gap = total - subtotal

    if abs(gap) <= tolerance:
        return Reconciliation(
            name, total, tuple(parts), 0.0, tolerance, "balanced",
            f"{name}: total {total:,.0f} equals its {len(known)} parts",
        )

    # The Cisco shape: the "total" is really one of the parts. Checked before
    # the residual branch, because a residual label would otherwise explain away
    # the most dangerous outcome there is.
    for part in known:
        others = [c for c in known if c is not part and c.value]
        if abs(total - (part.value or 0.0)) <= tolerance and others:
            return Reconciliation(
                name, total, tuple(parts), gap, tolerance, "scope_conflict",
                f"{name}: the reported total {total:,.0f} equals the part {part.name!r} "
                f"while {len(others)} other non-zero part(s) exist — it is a component "
                "presented as a total, not the total",
            )

    if residual_label is not None and (residual is None or abs(gap - residual) <= tolerance):
        return Reconciliation(
            name, total, tuple(parts), gap, tolerance, "residual",
            f"{name}: total {total:,.0f} exceeds its parts by {gap:+,.0f}, "
            f"identified as {residual_label}",
        )

    return Reconciliation(
        name, total, tuple(parts), gap, tolerance, "unreconciled",
        f"{name}: total {total:,.0f} against parts summing to {subtotal:,.0f} "
        f"leaves {gap:+,.0f} unexplained"
        + (f" (expected {residual_label} of {residual:+,.0f})" if residual is not None else ""),
    )


__all__ = ["Component", "Reconciliation", "ReconciliationStatus", "reconcile"]
