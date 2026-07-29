"""Cross-repo enforcement on the MOUNTED finance tool surface (audit Gate 2).

Package-level tests can each be green while the composed agent surface drifts:
this test builds the actual default finance tool set — every hub-backed
provider as an agent would mount it — and asserts the invariants the audit
demands hold on the mounted NAMES, not just inside each repo:

  1. no name collisions across providers;
  2. no raw-series tool in the default profile (CA-02);
  3. no direct-provider tool names at all (CA-03: edgar_*, prices_*);
  4. no file loaders (CA-11), no legacy refresh (CA-07);
  5. write tools appear ONLY when the write flags are set.
"""

from __future__ import annotations

import pytest

from lazytools.connectors.datahub import DataHubTools
from lazytools.connectors.regimes import RegimeTools
from lazytools.statistical_analysis import StatisticalAnalysisTools
from lazytools.testing import FakeDataHubBackend


class _NullStatsBackend:
    def load_returns(self, instruments, *, start="", end="", frequency="D"):
        raise NotImplementedError


def _mounted_names(*, write: bool = False, regime_db_path: str) -> set[str]:
    pytest.importorskip("lazystats.regimes")
    providers = [
        DataHubTools(FakeDataHubBackend(), allow_refresh=write),
        StatisticalAnalysisTools(_NullStatsBackend()),
        # db_path=regime_db_path: RegimeTools(allow_write=True) re-inits the
        # process-wide regime depot at construction time -- without an
        # explicit path here every run of this test used to repoint it at
        # the shared default depot (~/.lazytools/regime_depot.db).
        RegimeTools(allow_write=write, db_path=regime_db_path),
    ]
    names: list[str] = []
    for p in providers:
        names.extend(t.name for t in p.as_tools())
    assert len(names) == len(set(names)), "tool name collision across providers"
    return set(names)


def test_default_finance_surface_has_no_raw_no_direct_no_file_tools(tmp_path) -> None:
    names = _mounted_names(write=False, regime_db_path=str(tmp_path / "t.db"))
    # CA-02: no raw matrices by default
    assert "datahub_get_series" not in names
    assert "datahub_get_returns" not in names
    # CA-03: no direct-provider tools under any name
    assert not any(n.startswith(("edgar_", "prices_")) for n in names)
    # CA-07 / CA-11: no legacy refresh, no file loaders
    assert "datahub_refresh_prices" not in names
    assert not any("load_time_series" in n for n in names)
    # and no write capability leaked into the read profile
    assert not any(n.startswith(("datahub_ensure", "datahub_register",
                                 "regime_fit", "regime_load")) for n in names)


def test_write_finance_surface_is_the_ensure_register_fit_set(tmp_path) -> None:
    write_only = _mounted_names(write=True, regime_db_path=str(tmp_path / "w.db")) - _mounted_names(
        write=False, regime_db_path=str(tmp_path / "r.db")
    )
    assert write_only == {
        "datahub_register_listing",
        "datahub_ensure_price_history",
        "datahub_ensure_financials",
        "regime_init_db",
        "regime_load_from_datahub", "regime_generate_plots",
        "regime_fit", "regime_fit_categorical", "regime_fit_window",
        "regime_apply_params", "regime_store_delete", "regime_params_save",
        "regime_db_export_plot", "regime_db_delete_series",
        "regime_db_delete_result",
    }


def test_mounted_read_names_match_hub_read_bundle(tmp_path) -> None:
    """The datahub_* read names must map 1:1 onto the hub's TOOL_FUNCTIONS —
    the cross-repo drift guard on the actual mounted surface."""
    mdh = pytest.importorskip("market_data_hub.agent_tools")
    hub_read = {f.__name__.replace("tool_", "datahub_")
                for f in mdh.TOOL_FUNCTIONS}
    mounted = {n for n in _mounted_names(write=False, regime_db_path=str(tmp_path / "t.db"))
               if n.startswith("datahub_")}
    assert mounted == hub_read
