"""Reconciliation, against the two real filings that motivated it.

Cisco FY2024: the entity-wide fact for AmortizationOfIntangibleAssets is $698m,
which is the operating-expense slice; $955m more sits in cost of sales and the
true total is $1,653m. Nothing about the 698 gives it away.

Walmart FY2025: the debt components sum to $39,067m while the concept named
DebtLongtermAndShorttermCombinedAmount reports $35,999m. The $3,068m gap is
exactly ShortTermBorrowings -- the "combined" concept is a long-term subtotal.
"""

from __future__ import annotations

from lazytools.financials.reconcile import Component, reconcile

M = 1_000_000


# --- the Cisco shape: a component wearing a total's name ------------------- #


def test_a_total_that_equals_one_part_while_others_exist_is_a_scope_conflict() -> None:
    result = reconcile(
        "amortisation of purchased intangibles",
        total=698 * M,
        components={"cost of sales": 955 * M, "operating expenses": 698 * M},
    )
    assert result.status == "scope_conflict"
    assert result.blocking and not result.ok
    assert "presented as a total" in result.detail


def test_the_real_total_reconciles_against_the_same_parts() -> None:
    result = reconcile(
        "amortisation of purchased intangibles",
        total=1_653 * M,
        components={"cost of sales": 955 * M, "operating expenses": 698 * M},
    )
    assert result.status == "balanced" and result.ok


def test_a_residual_label_cannot_explain_away_a_scope_conflict() -> None:
    # Checked before the residual branch on purpose: labelling the gap would
    # otherwise launder the most dangerous outcome into a reported one.
    result = reconcile(
        "amortisation", total=698 * M,
        components={"cost of sales": 955 * M, "operating expenses": 698 * M},
        residual_label="something plausible",
    )
    assert result.status == "scope_conflict"


def test_a_total_equal_to_a_part_is_fine_when_the_others_are_zero() -> None:
    result = reconcile("x", total=100.0, components={"a": 100.0, "b": 0.0})
    assert result.status == "balanced"


# --- the Walmart shape: a subtotal named like a total ---------------------- #


def test_walmarts_combined_debt_concept_does_not_reconcile_to_gross_debt() -> None:
    result = reconcile(
        "gross financial debt",
        total=35_999 * M,
        components={"short-term borrowings": 3_068 * M,
                    "current long-term debt": 2_598 * M,
                    "non-current long-term debt": 33_401 * M},
    )
    assert result.status == "unreconciled"
    assert result.residual == -3_068 * M


def test_the_same_components_reconcile_to_the_true_gross_debt() -> None:
    result = reconcile(
        "gross financial debt",
        total=39_067 * M,
        components={"short-term borrowings": 3_068 * M,
                    "current long-term debt": 2_598 * M,
                    "non-current long-term debt": 33_401 * M},
    )
    assert result.status == "balanced"


def test_the_long_term_subtotal_reconciles_on_its_own_parts() -> None:
    # Which is how you learn what the "combined" concept actually is.
    result = reconcile(
        "long-term debt",
        total=35_999 * M,
        components={"current portion": 2_598 * M, "non-current portion": 33_401 * M},
    )
    assert result.status == "balanced"


# --- residuals -------------------------------------------------------------- #


def test_an_unexplained_gap_is_reported_not_absorbed() -> None:
    result = reconcile("debt", total=1_000.0, components={"a": 600.0, "b": 300.0})
    assert result.status == "unreconciled"
    assert result.residual == 100.0
    assert "unexplained" in result.detail


def test_a_named_residual_turns_an_unexplained_gap_into_a_reported_one() -> None:
    result = reconcile("debt", total=1_000.0, components={"a": 600.0, "b": 300.0},
                       residual_label="unamortised issuance costs")
    assert result.status == "residual" and result.ok
    assert "unamortised issuance costs" in result.detail


def test_a_named_residual_of_the_wrong_size_explains_nothing() -> None:
    result = reconcile("debt", total=1_000.0, components={"a": 600.0, "b": 300.0},
                       residual_label="issuance costs", residual=50.0)
    assert result.status == "unreconciled"


def test_a_named_residual_of_the_right_size_is_accepted() -> None:
    result = reconcile("debt", total=1_000.0, components={"a": 600.0, "b": 300.0},
                       residual_label="issuance costs", residual=100.0)
    assert result.status == "residual"


# --- missing parts ---------------------------------------------------------- #


def test_an_unread_part_blocks_rather_than_being_treated_as_zero() -> None:
    result = reconcile("lease liability", total=22_861 * M,
                       components={"current": None, "non-current": 17_437 * M})
    assert result.status == "incomplete" and result.blocking
    assert result.missing == ("current",)
    assert "not a zero one" in result.detail


def test_no_reported_total_is_its_own_state() -> None:
    # P&G serves no combined debt concept at all. That is not a failure to
    # reconcile; it is nothing to reconcile against.
    result = reconcile("debt", total=None, components={"a": 600.0, "b": 300.0})
    assert result.status == "no_total"
    assert not result.ok and not result.blocking


# --- rounding --------------------------------------------------------------- #


def test_rounding_drift_in_a_statement_rendered_in_millions_is_tolerated() -> None:
    # Five parts each rounded to the nearest million can drift several million
    # with nothing wrong.
    result = reconcile("x", total=1_000 * M,
                       components={f"p{i}": 200 * M for i in range(5)},
                       rounding_unit=M)
    assert result.status == "balanced"
    drifted = reconcile("x", total=1_003 * M,
                        components={f"p{i}": 200 * M for i in range(5)},
                        rounding_unit=M)
    assert drifted.status == "balanced"


def test_a_gap_wider_than_rounding_is_not_absorbed_by_the_tolerance() -> None:
    result = reconcile("x", total=1_010 * M,
                       components={f"p{i}": 200 * M for i in range(5)},
                       rounding_unit=M)
    assert result.status == "unreconciled"


def test_an_exact_source_gets_no_tolerance_by_default() -> None:
    result = reconcile("x", total=1_000.0, components={"a": 999.0})
    assert result.status == "unreconciled"


# --- shape ------------------------------------------------------------------ #


def test_components_may_be_passed_as_a_list_of_named_parts() -> None:
    result = reconcile("x", total=100.0,
                       components=[Component("a", 60.0), Component("b", 40.0)])
    assert result.status == "balanced"
    assert [c.name for c in result.known] == ["a", "b"]
