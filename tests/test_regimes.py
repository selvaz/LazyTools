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
    "regime_db_compare_results", "regime_db_get_state_sequence",
}
WRITE_NAMES = {
    "regime_init_db",
    "regime_load_from_datahub", "regime_generate_plots",
    "regime_fit", "regime_fit_categorical",
    "regime_fit_window", "regime_apply_params", "regime_store_delete",
    "regime_params_save", "regime_db_export_plot",
    "regime_db_delete_series", "regime_db_delete_result",
}


def test_provider_is_tool_provider() -> None:
    assert RegimeTools()._is_lazy_tool_provider is True


def test_read_tools_exposed_by_default() -> None:
    names = {t.name for t in RegimeTools().as_tools()}
    assert names == READ_NAMES


def test_write_tools_gated_by_allow_write(tmp_path) -> None:
    names = {t.name for t in RegimeTools(allow_write=True, db_path=str(tmp_path / "t.db")).as_tools()}
    assert names == READ_NAMES | WRITE_NAMES


def test_data_loading_is_hub_only_never_file_based(tmp_path) -> None:
    """Audit CA-11: an agent must never choose a filesystem path to load data
    from. The connector wraps ONLY the hub-backed loader; the file loader
    (lazystats.regimes.tools.load_time_series) is not reachable at all, gated
    or not."""
    all_names = {t.name for t in RegimeTools(allow_write=True, db_path=str(tmp_path / "t.db")).as_tools()}
    assert "regime_load_from_datahub" in all_names
    assert "regime_load_time_series" not in all_names
    assert not any("load_time_series" in n for n in all_names)


def test_load_from_datahub_tool_has_no_file_path_parameter(tmp_path) -> None:
    import inspect

    tools = {t.name: t for t in RegimeTools(allow_write=True, db_path=str(tmp_path / "t.db")).as_tools()}
    sig = inspect.signature(tools["regime_load_from_datahub"].func)
    assert "file_path" not in sig.parameters
    assert set(sig.parameters) >= {"symbols", "data_key"}


def _synthetic_two_state_series(n: int = 240, seed: int = 0) -> list[float]:
    import numpy as np

    rng = np.random.RandomState(seed)
    states = np.zeros(n, dtype=int)
    states[n // 3: 2 * n // 3] = 1
    x = np.where(states == 0, rng.normal(0, 0.5, n), rng.normal(0, 3.0, n))
    return [float(v) for v in x]


def test_load_from_datahub_tool_round_trips_through_the_hub_stub(monkeypatch, tmp_path) -> None:
    """End-to-end: symbols/dates in, bounded summary out (data_key, n_rows,
    ...) — never a raw series through the tool result."""
    import lazystats.regimes.datasources.datahub as _datahub_loader
    import pandas as pd

    def fake_extract_returns(symbols, start=None, end=None, frequency="D", **kw):
        idx = pd.to_datetime(["2024-01-05", "2024-01-12", "2024-01-19"])
        frame = pd.DataFrame({s: [0.01, -0.02, 0.03] for s in symbols}, index=idx)
        return frame, {"n_rows": 3}

    monkeypatch.setattr(_datahub_loader, "extract_returns", fake_extract_returns)

    tools = {t.name: t for t in RegimeTools(allow_write=True, db_path=str(tmp_path / "t.db")).as_tools()}
    out = tools["regime_load_from_datahub"].run_sync(
        symbols=["SPY"], data_key="test_hub_load")
    assert out["data_key"] == "test_hub_load"
    assert out["n_rows"] == 3
    assert out["source"] == "market-data-hub"
    assert "Y" not in out and "values" not in out  # bounded summary only


def test_fit_and_query_round_trip_through_wrapped_tools(tmp_path) -> None:
    """End-to-end: Tool.wrap's native Annotated-signature support is enough
    to make lazystats.regimes functions callable with no lazytools-side
    reimplementation of their schemas.

    db_path is a tmp_path depot, not the shared default -- RegimeTools
    (allow_write=True) re-inits the process-wide regime depot at construction
    time, so leaving this unset here used to fit_regimes() a real model
    straight into ~/.lazytools/regime_depot.db (the same depot production
    processes read from) every time this test ran."""
    tools = {t.name: t for t in RegimeTools(allow_write=True, db_path=str(tmp_path / "t.db")).as_tools()}

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


def test_state_sequence_and_changes_are_hard_capped(tmp_path) -> None:
    """Audit CA-12: last_n=0 ('return everything') must not reach the agent
    unbounded — the bridge clamps to a hard cap and reports truncation.

    db_get_state_sequence needs a SQLite depot backing store (fit_regimes
    persists there when given a result_key), so this test initializes one."""
    import lazystats.regimes.db as rdb

    from lazytools.connectors.regimes.tools import (
        _MAX_REGIME_CHANGES,
        _MAX_STATE_SEQUENCE_TIMESTEPS,
    )

    rdb.init_regime_db(str(tmp_path / "test_capped.db"))
    try:
        # A long, choppy series so both the state-sequence length and the
        # number of regime changes comfortably exceed the hard caps.
        n = _MAX_STATE_SEQUENCE_TIMESTEPS + 400
        data = [float(1.0 if i % 2 == 0 else -1.0) * (5.0 if i % 20 < 10 else 0.5)
                for i in range(n)]

        tools = {t.name: t for t in RegimeTools(allow_write=True, db_path=str(tmp_path / "test_capped.db")).as_tools()}
        fit_result = tools["regime_fit"].run_sync(
            data=data, series_names=["X"], result_key="test_capped",
            S_max=2, n_starts=1, random_state=0,
        )
        assert fit_result["n_timesteps"] == n

        seq = tools["regime_db_get_state_sequence"].run_sync(
            result_key="test_capped", series_name="X", last_n=0)
        assert seq["hard_cap"] == _MAX_STATE_SEQUENCE_TIMESTEPS
        assert seq["last_n"] == _MAX_STATE_SEQUENCE_TIMESTEPS
        assert len(seq["states"]) == _MAX_STATE_SEQUENCE_TIMESTEPS
        assert seq["truncated"] is True
        assert seq["n_timesteps"] == n

        changes = tools["regime_get_changes"].run_sync(
            result_key="test_capped", series_name="X", last_n=0)
        assert changes["hard_cap"] == _MAX_REGIME_CHANGES
        assert len(changes["changes"]) <= _MAX_REGIME_CHANGES
        if changes["n_changes"] > _MAX_REGIME_CHANGES:
            assert changes["truncated"] is True
            assert len(changes["changes"]) == _MAX_REGIME_CHANGES
    finally:
        rdb._DB = None


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


def test_every_tool_has_an_explicit_schema_description(tmp_path) -> None:
    """Every one of the 28 tools must carry a real, explicit description in
    its compiled schema — not a fallback derived from the wrapped lazystats
    function's docstring. Uniform with every other connector in this package
    (statistical_analysis, datahub, gmail, ...), which all pass description=
    explicitly rather than relying on docstring auto-derivation."""
    tools = {t.name: t for t in RegimeTools(allow_write=True, db_path=str(tmp_path / "t.db")).as_tools()}
    assert set(tools) == READ_NAMES | WRITE_NAMES
    for name, tool in tools.items():
        description = tool.definition().description
        assert description, f"{name} has no description in its schema"
        assert len(description) >= 15, f"{name} description is too thin: {description!r}"
