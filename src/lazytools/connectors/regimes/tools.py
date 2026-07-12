"""HMM/MS regime-detection tools from ``lazystats.regimes`` as LazyBridge tools.

``lazystats.regimes`` (the LazyHMM engines, migrated per plan v3.1 Fase 6) is
designed to expose plain functions with ``Annotated[type, "description"]``
parameters — LazyBridge's ``Tool.wrap`` reads that annotation natively, so
each tool here is a thin one-line wrap, not a reimplementation. This module
is the actual wiring point: before it existed, none of these ~25 functions
were reachable by an agent through LazyTools (they lived on their own with no
connector).

Read tools (inspection, no mutation of stored state) are always exposed.
Write tools (fit a model, persist/delete a stored result) require
``allow_write=True`` at construction — same gate discipline as every other
connector in this package.

Loading data: this connector deliberately wraps ONLY
``lazystats.regimes.load_from_datahub`` — symbols/dates/frequency/data_key in,
a bounded summary out, market-data-hub as the sole source (audit CA-11). It
does NOT expose ``lazystats.regimes.tools.load_time_series``, which reads an
arbitrary file path from disk: an agent must never choose what file to read.
That loader stays a notebook-only building block in ``lazystats`` itself, not
reachable through this (or any finance) connector.

``lazystats`` is imported lazily; a missing install raises a clear ImportError
with the ``[regimes]`` extra install hint.
"""

from __future__ import annotations

from typing import Any

# Hard caps enforced at the bridge (audit CA-12): the underlying lazystats
# functions accept last_n=0 meaning "return everything" — a TxS posterior
# matrix or a full change history can be as large as the whole modelled
# history. lazystats.core stays the source of truth for the math; capping
# LLM-facing output is this connector's job, same pattern as
# statistical_analysis's outlier cap.
_MAX_STATE_SEQUENCE_TIMESTEPS = 500
_MAX_REGIME_CHANGES = 200


class RegimeTools:
    """A ``ToolProvider`` exposing ``lazystats.regimes``'s tool layer.

    Regime fitting and the SQLite depot are process/session state (which
    series are loaded, which store backs persistence): unlike the stateless
    ``datahub_*`` tools, most of these tools read/write an in-process or
    SQLite-backed store shared across calls within one agent run — that is
    the whole point of ``regime_store_*`` / ``db_*`` / ``regime_params_*``.
    """

    _is_lazy_tool_provider = True

    def __init__(self, *, allow_write: bool = False) -> None:
        self._allow_write = allow_write

    # ------------------------------------------------------------------ #
    # Lazy access to lazystats.regimes (never imported at module level)
    # ------------------------------------------------------------------ #
    def _regimes(self) -> Any:
        try:
            import lazystats.regimes as _regimes
        except ImportError as exc:  # pragma: no cover - exercised without the extra
            raise ImportError(
                "lazytools.connectors.regimes requires lazystats[regimes]: "
                "pip install 'lazystats[regimes] @ "
                "git+https://github.com/selvaz/LazyStats.git'"
            ) from exc
        return _regimes

    def _db(self) -> Any:
        import lazystats.regimes.db as _db

        return _db

    # ------------------------------------------------------------------ #
    # Bounded wrappers (audit CA-12) — clamp last_n<=0/oversized requests to
    # a hard cap and report truncation, instead of forwarding "return
    # everything" straight to an agent.
    # ------------------------------------------------------------------ #
    def _get_regime_changes(self, result_key: str = "", series_name: str = "",
                            last_n: int = 0, fit_result: dict | None = None) -> dict:
        effective = last_n if last_n and 0 < last_n <= _MAX_REGIME_CHANGES \
            else _MAX_REGIME_CHANGES
        out = self._regimes().get_regime_changes(
            result_key=result_key, series_name=series_name,
            last_n=effective, fit_result=fit_result)
        out["hard_cap"] = _MAX_REGIME_CHANGES
        out["truncated"] = len(out["changes"]) < out["n_changes"]
        return out

    def _get_state_sequence(self, result_key: str, series_name: str,
                            last_n: int = 52) -> dict:
        effective = last_n if last_n and 0 < last_n <= _MAX_STATE_SEQUENCE_TIMESTEPS \
            else _MAX_STATE_SEQUENCE_TIMESTEPS
        out = self._db().db_get_state_sequence(
            result_key, series_name, last_n=effective)
        out["hard_cap"] = _MAX_STATE_SEQUENCE_TIMESTEPS
        out["truncated"] = out["last_n"] < out["n_timesteps"]
        return out

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Any]:
        from lazybridge import Tool

        r, db = self._regimes(), self._db()
        read = [
            # -- fitting (in-memory result, no persistence side effect unless
            #    result_key/data_key ask for it — those variants are gated below)
            (r.scan_state_counts, "regime_scan_state_counts"),
            (r.get_current_regime, "regime_get_current"),
            (self._get_regime_changes, "regime_get_changes"),
            (r.get_regime_summary, "regime_get_summary"),
            (r.compare_emission_models, "regime_compare_emission_models"),
            (r.compare_regime_windows, "regime_compare_windows"),
            # -- store / params inspection
            (r.regime_store_list, "regime_store_list"),
            (r.regime_store_load, "regime_store_load"),
            (r.regime_params_list, "regime_params_list"),
            (r.regime_params_load, "regime_params_load"),
            # -- SQLite depot inspection
            (db.db_list_series, "regime_db_list_series"),
            (db.db_get_series_info, "regime_db_get_series_info"),
            (db.db_list_results, "regime_db_list_results"),
            (db.db_get_result_summary, "regime_db_get_result_summary"),
            (db.db_list_plots, "regime_db_list_plots"),
            (db.db_compare_results, "regime_db_compare_results"),
            (self._get_state_sequence, "regime_db_get_state_sequence"),
        ]
        write = [
            # -- data loading: ONLY the hub-backed loader (audit CA-11). Never
            # lazystats.regimes.tools.load_time_series (arbitrary file_path).
            (r.load_from_datahub, "regime_load_from_datahub"),
            # -- fitting that persists a new result / consumes fitting time
            (r.fit_regimes, "regime_fit"),
            (r.fit_categorical_regimes, "regime_fit_categorical"),
            (r.fit_regimes_window, "regime_fit_window"),
            (r.apply_regime_params, "regime_apply_params"),
            # -- store / params mutation
            (r.regime_store_delete, "regime_store_delete"),
            (r.regime_params_save, "regime_params_save"),
            # -- SQLite depot mutation (db_export_plot writes a PNG to an
            # arbitrary filesystem path — a filesystem write, not read-only)
            (db.db_export_plot, "regime_db_export_plot"),
            (db.db_delete_series, "regime_db_delete_series"),
            (db.db_delete_result, "regime_db_delete_result"),
        ]
        descriptions = {
            "regime_get_changes": (
                "Dates of regime changes for one series, hard-capped at "
                f"{_MAX_REGIME_CHANGES} most recent changes (summary fields "
                "n_changes/current_state/last_change_date always reflect the "
                "full history; `truncated` says whether `changes` was cut). "
                "Args: result_key (preferred) or fit_result; series_name; "
                "last_n (optional, clamped to the hard cap)."
            ),
            "regime_db_get_state_sequence": (
                "Viterbi state sequence + posteriors for one series, "
                f"hard-capped at {_MAX_STATE_SEQUENCE_TIMESTEPS} timesteps "
                "(most recent). `truncated` says whether the stored history "
                "is longer than what was returned. Args: result_key; "
                "series_name; last_n (optional, default 52, clamped to the "
                "hard cap)."
            ),
            "regime_load_from_datahub": (
                "Load a log-returns matrix for one or more symbols from "
                "market-data-hub and store it under a data_key for the "
                "fitting tools (regime_fit(data_key=...), etc). The ONLY "
                "data-loading tool in this connector — no file path, no raw "
                "series in the response, just a bounded summary. Args: "
                "symbols (str or list, e.g. 'SPY' or ['SPY','TLT']); start, "
                "end (ISO dates, optional); frequency (default 'W'); field "
                "(price field, default 'adj_close'); fillna (missing-value "
                "policy, default 'none'); data_key (depot key, default "
                "'datahub'). Returns: data_key, n_rows, n_cols, columns, "
                "date_range, source."
            ),
        }
        tools = [Tool.wrap(fn, name=name, description=descriptions.get(name))
                 for fn, name in read]
        if self._allow_write:
            tools += [Tool.wrap(fn, name=name, description=descriptions.get(name))
                     for fn, name in write]
        return tools
