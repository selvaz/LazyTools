"""Every statistical_* tool's schema must carry per-parameter descriptions.

LazyBridge's default signature-mode schema builder parses a Google-style
``Args:`` docstring section for per-parameter descriptions (one physical
line each — continuation lines are silently dropped, see
lazybridge/core/tool_schema.py `_parse_docstring_params`). This guards that
every parameter of every tool actually reaches an agent with a real
description, not just a bare name/type.
"""

from __future__ import annotations

import pytest

pytest.importorskip("lazystats", reason="the statistical tools delegate to lazystats")
pytest.importorskip("statsmodels", reason="regression needs lazystats[regression]")
pytest.importorskip("sklearn", reason="regression needs lazystats[regression]")

from lazytools.statistical_analysis import StatisticalAnalysisTools


class _StubBackend:
    def load_series(self, specs, *, start="", end="", frequency="D"):  # pragma: no cover
        raise NotImplementedError

    def load_returns(self, instruments, *, start="", end="", frequency="D"):  # pragma: no cover
        raise NotImplementedError


def _definitions():
    provider = StatisticalAnalysisTools(_StubBackend())
    return {tool.name: tool.definition() for tool in provider.as_tools()}


@pytest.mark.parametrize(
    "tool_name",
    [
        "statistical_return_volatility",
        "statistical_return_correlation",
        "statistical_return_outliers",
        "statistical_regression_ols",
        "statistical_regression_ridge",
        "statistical_regression_lasso",
    ],
)
def test_every_parameter_has_a_description(tool_name: str) -> None:
    definition = _definitions()[tool_name]
    properties = definition.parameters["properties"]
    assert properties, f"{tool_name} exposes no parameters"
    for param_name, prop in properties.items():
        description = prop.get("description")
        assert description, f"{tool_name}.{param_name} has no description in its schema"
        assert len(description) >= 15, f"{tool_name}.{param_name} description is too thin"


def test_top_level_descriptions_are_not_docstring_truncated() -> None:
    """The explicit description= must be the full text, not just the first line.

    Regression guard: if description= were ever dropped in favour of pure
    docstring auto-derivation, LazyBridge would silently fall back to only
    the docstring's first line (see build_artifact in tool_schema.py) —
    this asserts the full multi-clause description is still present.
    """
    definitions = _definitions()
    ols = definitions["statistical_regression_ols"]
    assert "AIC/BIC" in ols.description
    assert "residual diagnostics" in ols.description
    ridge = definitions["statistical_regression_ridge"]
    assert "cross-validated" in ridge.description


def test_regression_tool_docstrings_have_args_and_returns_sections() -> None:
    """Source-level docstrings stay complete for humans (help(), IDE hover)."""
    provider = StatisticalAnalysisTools(_StubBackend())
    for method_name in ("_regression_ols", "_regression_ridge", "_regression_lasso"):
        doc = getattr(provider, method_name).__doc__
        assert doc is not None
        assert "Args:" in doc
        assert "Returns:" in doc
