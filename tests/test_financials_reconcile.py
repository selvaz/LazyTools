"""Reconciliation, against the two real filings that motivated it.

Cisco FY2024: the entity-wide fact for AmortizationOfIntangibleAssets is $698m,
which is the operating-expense slice; $955m more sits in cost of sales and the
true total is $1,653m. Nothing about the 698 gives it away.

Walmart FY2025: the debt components sum to $39,067m while the concept named
DebtLongtermAndShorttermCombinedAmount reports $35,999m. The $3,068m gap is
exactly ShortTermBorrowings -- the "combined" concept is a long-term subtotal.
"""

from __future__ import annotations

import math

from lazytools.financials.reconcile import reconcile

M = 1_000_000


# --- the Cisco shape: a component wearing a total's name ------------------- #


def test_a_total_that_equals_one_part_while_others_exist_is_a_scope_conflict() -> None:
    result = reconcile("amortisation", total=698 * M,
                       components={"cost of sales": 955 * M, "operating expenses": 698 * M})
    assert result.status == "scope_conflict" and result.blocking
    assert "presented as a total" in result.detail


def test_the_real_total_reconciles_against_the_same_parts() -> None:
    result = reconcile("amortisation", total=1_653 * M,
                       components={"cost of sales": 955 * M, "operating expenses": 698 * M})
    assert result.status == "balanced" and not result.blocking


def test_a_residual_that_does_not_match_cannot_launder_a_scope_conflict() -> None:
    result = reconcile("amortisation", total=698 * M,
                       components={"cost of sales": 955 * M, "operating expenses": 698 * M},
                       residual=("something plausible", -100 * M))
    assert result.status == "scope_conflict"


def test_a_mis_scoped_total_that_matches_no_single_part_is_merely_unreconciled() -> None:
    # A known limitation, asserted so it is a decision rather than a surprise:
    # the scope heuristic only fires on an exact-ish match with one part.
    result = reconcile("amortisation", total=699 * M,
                       components={"cost of sales": 955 * M, "operating expenses": 698 * M})
    assert result.status == "unreconciled"


# --- the Walmart shape: a subtotal named like a total ---------------------- #


WMT = {"short-term borrowings": 3_068 * M,
       "current long-term debt": 2_598 * M,
       "non-current long-term debt": 33_401 * M}


def test_walmarts_combined_debt_concept_does_not_reconcile_to_gross_debt() -> None:
    result = reconcile("gross financial debt", total=35_999 * M, components=WMT)
    assert result.status == "unreconciled"
    assert result.gap == -3_068 * M


def test_the_same_concept_reconciles_against_the_long_term_parts_alone() -> None:
    # Which is how you learn what the concept actually is.
    result = reconcile("long-term debt", total=35_999 * M,
                       components={k: v for k, v in WMT.items() if "long-term" in k})
    assert result.status == "balanced"


# --- residuals -------------------------------------------------------------- #


def test_an_unexplained_gap_is_reported_not_absorbed() -> None:
    result = reconcile("debt", total=1_000.0, components={"a": 600.0, "b": 300.0})
    assert result.status == "unreconciled" and result.gap == 100.0


def test_a_disclosed_residual_of_the_right_size_explains_the_gap() -> None:
    result = reconcile("debt", total=1_000.0, components={"a": 600.0, "b": 300.0},
                       residual=("unamortised issuance costs", 100.0))
    assert result.status == "residual" and not result.blocking


def test_a_disclosed_residual_of_the_wrong_size_explains_nothing() -> None:
    result = reconcile("debt", total=1_000.0, components={"a": 600.0, "b": 300.0},
                       residual=("issuance costs", 50.0))
    assert result.status == "unreconciled"
    assert "was expected" in result.detail


def test_a_residual_cannot_be_named_without_being_quantified() -> None:
    # A label alone would accept any gap: "other" absorbing 999 on a total of
    # 1,000. The type makes that unexpressible.
    result = reconcile("debt", total=1_000.0, components={"a": 1.0},
                       residual=("other", 999.0))
    assert result.status == "residual"  # only because it is quantified AND matches


def test_offsetting_parts_do_not_trigger_a_false_scope_conflict() -> None:
    # total equals "principal" exactly, and premium/discount net to zero against
    # each other. A quantified residual that matches wins over the heuristic.
    result = reconcile("debt", total=100.0,
                       components={"principal": 100.0, "premium": 10.0},
                       residual=("discount", -10.0))
    assert result.status == "residual"


# --- unusable inputs -------------------------------------------------------- #


def test_an_unread_part_blocks_rather_than_being_treated_as_zero() -> None:
    result = reconcile("lease liability", total=22_861 * M,
                       components={"current": None, "non-current": 17_437 * M})
    assert result.status == "incomplete" and result.blocking
    assert "not a zero one" in result.detail


def test_a_nan_part_is_unreadable_rather_than_arithmetic() -> None:
    # NaN makes every comparison false, so a NaN gap would slip past every
    # threshold and be reported as whichever branch came last.
    result = reconcile("x", total=100.0, components={"a": float("nan"), "b": 60.0})
    assert result.status == "incomplete"


def test_an_infinite_total_is_no_total() -> None:
    assert reconcile("x", total=math.inf, components={"a": 1.0}).status == "no_total"


def test_no_reported_total_is_its_own_state() -> None:
    # P&G serves no combined debt concept at all. Nothing to reconcile against
    # is not a failure to reconcile.
    result = reconcile("debt", total=None, components={"a": 600.0, "b": 300.0})
    assert result.status == "no_total" and not result.blocking


# --- rounding --------------------------------------------------------------- #


def _five_parts(total_millions: float):
    return reconcile("x", total=total_millions * M,
                     components={f"p{i}": 200 * M for i in range(5)}, rounding_unit=M)


def test_the_rounding_bound_is_half_a_unit_per_part_plus_the_total() -> None:
    # Five parts and a total, each rounded to the nearest million, drift by at
    # most 6 * 0.5 = 3 million. Not 5, which the earlier formula allowed.
    assert _five_parts(1_003).status == "balanced"
    assert _five_parts(1_004).status == "unreconciled"


def test_an_exact_source_gets_no_tolerance_by_default() -> None:
    assert reconcile("x", total=1_000.0, components={"a": 999.0}).status == "unreconciled"


def test_the_scope_test_uses_the_pairwise_bound_not_the_aggregate_one() -> None:
    # Parts sum to 106 against a total of 98, so this is not balanced. The total
    # sits 2 away from part "a" -- inside the OLD tolerance of unit x parts (2)
    # and outside the correct pairwise bound of one unit, so it must NOT be
    # called a scope conflict.
    result = reconcile("x", total=98.0, components={"a": 96.0, "b": 10.0},
                       rounding_unit=1.0)
    assert result.status == "unreconciled"


def test_the_scope_test_still_fires_within_the_pairwise_bound() -> None:
    result = reconcile("x", total=97.0, components={"a": 96.0, "b": 10.0},
                       rounding_unit=1.0)
    assert result.status == "scope_conflict"
