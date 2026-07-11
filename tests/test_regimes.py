"""connectors/regimes — the lazystats.regimes tool layer wired into an agent.

Before this connector existed, none of these functions were reachable by an
LLM agent through LazyTools (plan v3.1 Fase 6 completeness check)."""

from __future__ import annotations

import pytest

pytest.importorskip("lazystats.regimes", reason="regimes connector requires "
                    "lazystats[regimes]")

from lazytools.connectors.regimes import RegimeTools

READ_NAMES = {
    "regime_scan_state_counts", "regime_get_current", "regime_get_changes",
    "regime_get_summary", "regime_compare_emission_models",
    "regime_compare_windows", "regime_store_list", "regime_store_load",
    "regime_params_list", "regime_params_load", "regime_db_list_series",
    "regime_db_get_series_info", "regime_db_list_results",
    "regime_db_get_result_summary", "regime_db_list_plots",
    "regime_db_export_plot", "regime_db_compare_results",
    "regime_db_get_state_sequence",
}
WRITE_NAMES = {
    "regime_load_time_series", "regime_fit", "regime_fit_categorical",
    "regime_fit_window", "regime_apply_params", "regime_store_delete",
    "regime_params_save", "regime_db_delete_series", "regime_db_delete_result",
}


def test_provider_is_tool_provider() -> None:
    assert RegimeTools()._is_lazy_tool_provider is True


def test_read_tools_exposed_by_default() -> None:
    names = {t.name for t in RegimeTools().as_tools()}
    assert names == READ_NAMES


def test_write_tools_gated_by_allow_write() -> None:
    names = {t.name for t in RegimeTools(allow_write=True).as_tools()}
    assert names == READ_NAMES | WRITE_NAMES


def _synthetic_two_state_series(n: int = 240, seed: int = 0) -> list[float]:
    import numpy as np

    rng = np.random.RandomState(seed)
    states = np.zeros(n, dtype=int)
    states[n // 3: 2 * n // 3] = 1
    x = np.where(states == 0, rng.normal(0, 0.5, n), rng.normal(0, 3.0, n))
    return [float(v) for v in x]


def test_fit_and_query_round_trip_through_wrapped_tools() -> None:
    """End-to-end: Tool.wrap's native Annotated-signature support is enough
    to make lazystats.regimes functions callable with no lazytools-side
    reimplementation of their schemas."""
    tools = {t.name: t for t in RegimeTools(allow_write=True).as_tools()}

    # documented LLM workflow: fit with a result_key, then query by that key
    # (the compact fit_result returned inline strips per-timestep arrays;
    # only the result_key-backed store keeps what get_current_regime needs)
    fit_result = tools["regime_fit"].run_sync(
        data=_synthetic_two_state_series(), series_names=["SPY"],
        result_key="test_spy_regimes", S_max=3, n_starts=2, random_state=0,
    )
    assert set(fit_result["series"]) == {"SPY"}
    assert fit_result["result_key"] == "test_spy_regimes"

    current = tools["regime_get_current"].run_sync(
        result_key="test_spy_regimes", series_name="SPY")
    assert "current_state" in current

    changes = tools["regime_get_changes"].run_sync(
        result_key="test_spy_regimes", series_name="SPY")
    assert isinstance(changes, dict)


def test_missing_lazystats_regimes_raises_clear_import_error(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "lazystats.regimes" or name.startswith("lazystats.regimes."):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    with pytest.raises(ImportError, match=r"lazystats\[regimes\]"):
        RegimeTools().as_tools()
