"""Checking a total against the parts it is supposed to be made of.

Every silent error this codebase has hit had the same shape: a number that
resolved cleanly, carried full provenance, and was not what it claimed to be.
Provenance does not protect against that. Arithmetic does — not by proving a
figure right, which nothing can do from a single fact, but by refusing to let an
aggregate pass unchecked when the filing also discloses its parts.

The outcomes are different findings and must not collapse into "it didn't add
up". ``balanced`` is the strongest statement available. ``residual`` is common
and fine once the difference is both named and quantified. ``unreconciled`` is a
fact about the filing, to report rather than swallow. And ``scope_conflict`` is
the dangerous one: the reported total equals ONE part while other non-zero parts
exist — Cisco's ``AmortizationOfIntangibleAssets`` at $698m looks like a total
and is the operating-expense slice, with $955m more in cost of sales.

Tolerance is for **rounding, not economics**. A statement rendered in millions
cannot resolve below a million, so N parts and their total can each be off by
half a unit: the bound is ``(N + 1) × unit / 2``. Comparing the total against a
single part involves only two rounded values, so that bound is one unit. A
tolerance wide enough to absorb a real discrepancy is not a tolerance, it is a
way of not noticing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

ReconciliationStatus = Literal[
    "balanced", "residual", "scope_conflict", "unreconciled", "incomplete", "no_total"
]


@dataclass(frozen=True)
class Reconciliation:
    """What a total and its parts say about each other."""

    name: str
    total: float | None
    components: dict[str, float | None]
    #: total − sum(parts), or ``None`` when there was nothing to compare.
    gap: float | None
    status: ReconciliationStatus
    detail: str

    @property
    def blocking(self) -> bool:
        """Anything built on this total would rest on an unchecked number."""
        return self.status in ("scope_conflict", "unreconciled", "incomplete")


def reconcile(
    name: str,
    total: float | None,
    components: dict[str, float | None],
    *,
    rounding_unit: float = 0.0,
    residual: tuple[str, float] | None = None,
) -> Reconciliation:
    """Test ``total`` against ``components``, which must be mutually exclusive.

    Args:
        name: what is being reconciled, for the message.
        total: the reported total, or ``None`` when the filing states none.
        components: the disjoint parts. A ``None`` value means a part exists but
            could not be read — which is not the same as it being zero, and is
            why that case blocks rather than reconciling.
        rounding_unit: the smallest amount the source can express — ``1_000_000``
            for a statement rendered in millions, ``0`` for exact sources such as
            XBRL facts.
        residual: a difference that is expected, as ``(name, amount)``. Both are
            required together on purpose: a label alone would accept any gap as
            explained, so "other" could absorb a 999 plug on a total of 1,000.

    Components are assumed **mutually exclusive**. Passing a total alongside its
    own subtotal double counts, and nothing here can detect it: choosing parts
    that do not overlap is the caller's job.
    """
    readable = {k: v for k, v in components.items() if _is_readable(v)}
    unreadable = [k for k in components if k not in readable]

    if total is None or not _is_readable(total):
        return _result(name, None, components, None, "no_total",
                       f"{name}: no reported total to check the parts against")
    if unreadable:
        return _result(name, total, components, None, "incomplete",
                       f"{name}: {len(unreadable)} part(s) unreadable "
                       f"({', '.join(unreadable)}), so the total cannot be confirmed — "
                       "an unread part is not a zero one")
    if not readable:
        return _result(name, total, components, None, "incomplete",
                       f"{name}: no parts to check the total against")

    subtotal = sum(readable.values())  # type: ignore[arg-type]
    gap = total - subtotal
    # N parts plus the total, each rounded to the nearest unit, drift by at most
    # half a unit each.
    aggregate_tolerance = abs(rounding_unit) * (len(readable) + 1) / 2
    # One total against one part is two rounded values, so one unit.
    pairwise_tolerance = abs(rounding_unit)

    if abs(gap) <= aggregate_tolerance:
        return _result(name, total, components, gap, "balanced",
                       f"{name}: total {total:,.0f} equals its {len(readable)} parts")

    if residual is not None and abs(gap - residual[1]) <= aggregate_tolerance:
        # Checked before the scope test: an independently quantified difference
        # that matches is stronger evidence than the coincidence heuristic, and
        # a total legitimately equal to one part happens whenever the others
        # offset each other.
        return _result(name, total, components, gap, "residual",
                       f"{name}: total {total:,.0f} differs from its parts by {gap:+,.0f}, "
                       f"matching the disclosed {residual[0]}")

    for part, value in readable.items():
        others = [v for k, v in readable.items() if k != part and v]
        if abs(total - (value or 0.0)) <= pairwise_tolerance and others:
            return _result(name, total, components, gap, "scope_conflict",
                           f"{name}: the reported total {total:,.0f} equals the part "
                           f"{part!r} while {len(others)} other non-zero part(s) exist — "
                           "it is a component presented as a total, not the total")

    expected = f" (a {residual[0]} of {residual[1]:+,.0f} was expected)" if residual else ""
    return _result(name, total, components, gap, "unreconciled",
                   f"{name}: total {total:,.0f} against parts summing to {subtotal:,.0f} "
                   f"leaves {gap:+,.0f} unexplained{expected}")


def _result(name, total, components, gap, status, detail) -> Reconciliation:  # noqa: ANN001
    return Reconciliation(name=name, total=total, components=dict(components),
                          gap=gap, status=status, detail=detail)


def _is_readable(value: float | None) -> bool:
    """A value that can be arithmetic. ``NaN`` and infinities cannot.

    Left out of the sum rather than propagated: a NaN makes every comparison
    false, so a gap of NaN would slip past every threshold and be reported as
    whatever branch happened to come last.
    """
    return value is not None and math.isfinite(value)


__all__ = ["Reconciliation", "ReconciliationStatus", "reconcile"]
