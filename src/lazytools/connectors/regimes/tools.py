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

    def __init__(self, *, allow_write: bool = False, db_path: str | None = None) -> None:
        self._allow_write = allow_write
        self._db_path = db_path
        # The SQLite depot backs chart storage (regime_generate_plots) and every
        # regime_db_* tool. In write mode, open a default depot up-front so that
        # pipeline works out of the box; callers can re-point it via
        # regime_init_db. Never fatal — if the extra is missing the provider is
        # skipped later at as_tools() anyway.
        if allow_write:
            try:
                self._db().init_regime_db(self._resolve_db_path())
            except Exception:  # pragma: no cover - depot is best-effort at construction
                pass

    def _resolve_db_path(self) -> str:
        import os

        if self._db_path:
            return self._db_path
        env = os.environ.get("LAZYTOOLS_REGIME_DB")
        if env:
            return env
        base = os.environ.get("LAZYTOOLS_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".lazytools")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "regime_depot.db")

    def _init_db(self, db_path: str = "") -> dict:
        """Create / open the SQLite depot that backs regime_generate_plots and
        the regime_db_* tools. Returns the resolved path."""
        path = db_path or self._resolve_db_path()
        self._db().init_regime_db(path)
        return {"status": "ok", "db_path": path}

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
            # -- depot lifecycle: create/open the SQLite depot that backs chart
            # storage and every regime_db_* tool (write mode auto-opens a default
            # one at construction; this re-points it).
            (self._init_db, "regime_init_db"),
            # -- data loading: ONLY the hub-backed loader (audit CA-11). Never
            # lazystats.regimes.tools.load_time_series (arbitrary file_path).
            (r.load_from_datahub, "regime_load_from_datahub"),
            # -- chart generation into the depot (writes PNG blobs there;
            # export to disk stays behind regime_db_export_plot)
            (r.generate_regime_plots, "regime_generate_plots"),
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
            "regime_init_db": (
                "Create or open the SQLite depot that backs regime_generate_plots "
                "and every regime_db_* tool. Write mode opens a default depot at "
                "startup; call this only to re-point it. Args: db_path (optional; "
                "empty uses LAZYTOOLS_REGIME_DB or ~/.lazytools/regime_depot.db)."
            ),
            "regime_scan_state_counts": (
                "Scan candidate regime counts (S) and return BIC/AIC/HQIC selection "
                "scores without fitting a final model — run this before regime_fit "
                "to audit which S values are viable and spot the BIC elbow. Args: "
                "data (Txk nested list), series_names, model ('panel' scans each "
                "series independently; 'joint_diag'/'joint_full' scan jointly), "
                "S_max (default 6), S_min (default 1), criterion, n_starts."
            ),
            "regime_get_current": (
                "Return the current (most recent timestep) regime for one series: "
                "state index/label, posterior probabilities, mean/volatility, "
                "expected remaining duration, and the one-step transition "
                "probability to the high-vol regime. Args: result_key (preferred, "
                "from regime_fit) or fit_result; series_name."
            ),
            "regime_get_summary": (
                "Return a human-readable, plain-text summary of all fitted regimes "
                "for a stored result — per-regime mean/volatility/occupancy/duration "
                "and the current regime per series. Designed for direct LLM "
                "consumption (text, not structured data). Args: result_key (from "
                "regime_fit) or fit_result."
            ),
            "regime_compare_emission_models": (
                "Fit and compare three emission-model variants at a fixed regime "
                "count S — 'full' (per-regime mean and covariance), 'diag' "
                "(per-regime mean and diagonal variance), 'diag_shared_mean' "
                "(shared mean, variance-only regimes) — to choose the right model "
                "before calling regime_fit. Args: data, series_names, S (fixed "
                "regime count, pick from regime_scan_state_counts), model "
                "('panel'|'joint_diag'|'joint_full'), n_starts, random_state."
            ),
            "regime_compare_windows": (
                "Fit independent regime models on multiple date-sliced observation "
                "windows and compare regime counts, per-regime stats, transition "
                "persistence and BIC across them — use to detect parameter drift or "
                "regime-count changes across historical sub-periods. Args: data, "
                "series_names, windows (list of {label, start, end} dicts), model, "
                "S_max (default 4), S_min (default 1), criterion, n_starts, "
                "shared_mean, sticky, random_state."
            ),
            "regime_store_list": (
                "List all keys currently held in the in-process regime store (fit "
                "results, loaded data, parameter records) with a total count. "
                "Takes no arguments."
            ),
            "regime_store_load": (
                "Peek at an object stored in the regime store — its Python type, "
                "shape/size and a human-readable summary — without loading or "
                "returning the full object. Args: key (store key to inspect)."
            ),
            "regime_params_list": (
                "Discover trained model parameters already saved in the store — "
                "call this before regime_fit to check whether a ready-to-use model "
                "already exists and avoid refitting; returns compact provenance "
                "metadata only, no parameter arrays. Args: data_key (optional "
                "filter; empty = all)."
            ),
            "regime_params_load": (
                "Load a stored parameter record (startprob/transmat/means/"
                "covariances plus data provenance) for reuse via "
                "regime_apply_params, without refitting. Args: params_key "
                "('<result_key>::params' or the params_key returned by regime_fit)."
            ),
            "regime_generate_plots": (
                "Render every regime chart for a stored fit result — one series-"
                "with-regimes plot per series, plus two barcode charts — into the "
                "SQLite depot as PNGs, headlessly. Use regime_db_list_plots to "
                "enumerate them and regime_db_export_plot to save one to disk. "
                "Requires an active depot. Args: result_key; data_key (optional, "
                "defaults to the data_key recorded in the fit); theme "
                "('dark'|'light'|'minimal'); last_years (default 20); "
                "points_per_year (default 52)."
            ),
            "regime_fit": (
                "Fit a Gaussian HMM to detect volatility regimes in financial time "
                "series, automatically selecting the regime count via BIC/AIC/HQIC "
                "(regimes always ordered by volatility, state 0 = calmest). Before "
                "calling this, check regime_params_list(data_key=...) for an "
                "existing trained model and prefer regime_apply_params over "
                "refitting when one exists — fitting is expensive. When result_key "
                "is given, this auto-persists the parameters for reuse. Args: "
                "data_key (preferred, from regime_load_from_datahub) or "
                "data/series_names; result_key (store key for downstream tools); "
                "model ('panel'|'joint_diag'|'joint_full'); S_max (default 4), "
                "S_min (default 1), criterion, n_starts, shared_mean, sticky, "
                "random_state."
            ),
            "regime_fit_categorical": (
                "Fit a discrete-emission HMM on categorical/integer observations "
                "(e.g. quantile buckets, sentiment scores, volatility tiers) instead "
                "of continuous returns — the emission model is a learned "
                "probability table per state, and the regime count is selected "
                "automatically via BIC. Args: observations (0-indexed ints, or a "
                "Txn_features nested list for multiple features); S_max (default "
                "5), S_min (default 1), n_starts, n_iter, random_state."
            ),
            "regime_fit_window": (
                "Fit regime detection on a specific contiguous date-sliced window "
                "of the data — identical to regime_fit but scoped to "
                "[window_start:window_end], useful for a historical sub-period or "
                "for feeding regime_compare_windows. Args: data, series_names, "
                "window_start (inclusive, negative = from end), window_end "
                "(exclusive, 0 = end of data), model, S_max, S_min, criterion, "
                "n_starts, shared_mean, sticky, random_state."
            ),
            "regime_apply_params": (
                "Apply previously-fitted, stored model parameters to (new) data via "
                "fixed-parameter inference — decodes regimes and posteriors without "
                "refitting; series are matched to the trained model by name. Args: "
                "params_key (from regime_fit or regime_params_save); data, "
                "series_names (or data_key); result_key (optional, to store the "
                "output)."
            ),
            "regime_store_delete": (
                "Delete one key from the in-process regime store to free memory. "
                "Args: key."
            ),
            "regime_params_save": (
                "Explicitly (re)persist the parameter record for an existing fit "
                "result — regime_fit already auto-saves when given a result_key, so "
                "use this only to save under a different key or after manual edits. "
                "Args: result_key (existing fit result); params_key (optional, "
                "defaults to '<result_key>::params')."
            ),
            "regime_db_list_series": (
                "List every time series stored in the SQLite regime depot, with "
                "per-series row/column counts and date range, plus a total count. "
                "Takes no arguments."
            ),
            "regime_db_get_series_info": (
                "Return detailed metadata for one stored time series in the depot "
                "— columns, row/date range, fill policy and per-column ticker "
                "provenance when loaded via a ticker loader. Args: data_key (get "
                "keys from regime_db_list_series)."
            ),
            "regime_db_list_results": (
                "List every stored HMM fit result in the depot, with per-series "
                "regime count/BIC/current-regime summaries and a total count. "
                "Takes no arguments."
            ),
            "regime_db_get_result_summary": (
                "Return a compact regime summary for one stored fit result — regime "
                "stats, transition matrix, BIC, current regime per series — without "
                "the underlying T-length state sequence; use "
                "regime_db_get_state_sequence for the full sequence. Args: "
                "result_key (get keys from regime_db_list_results)."
            ),
            "regime_db_list_plots": (
                "List every plot stored in the depot, with per-plot type, title, "
                "dimensions and the result/data key it was generated from, plus a "
                "total count. Takes no arguments."
            ),
            "regime_db_compare_results": (
                "Compare regime statistics — regime count, BIC, current regime "
                "label, per-regime stats — across multiple stored fit results, "
                "aligned by series name. Args: result_keys (list of stored result "
                "keys to compare, e.g. ['spy_2y', 'spy_5y'])."
            ),
            "regime_db_export_plot": (
                "Save one stored plot PNG from the depot to a filesystem path — the "
                "only tool in this connector that writes to disk. Args: plot_key "
                "(get keys from regime_db_list_plots); output_path (directory must "
                "already exist)."
            ),
            "regime_db_delete_series": (
                "Delete one stored time series (and its data) from the SQLite "
                "depot. Args: data_key."
            ),
            "regime_db_delete_result": (
                "Delete one stored fit result, including its state-sequence rows, "
                "from the SQLite depot. Args: result_key."
            ),
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
