"""LazyBridge tool provider over LazyPortfolio's hierarchical (V2) tree engine.

Where ``PortfolioOptimizationTools`` (``tools.py``) wraps a single flat node,
``PortfolioTreeTools`` exposes the full node-tree configuration (parent/child
hierarchies, per-node proxies, ``flat``/``forward``/``forward_backward``
modes) — the surface that class's own docstring says is "only exposed
through Tree Studio / ``V2Model.from_config`` directly". A tree here is the
exact same JSON object Tree Studio's visual editor edits and saves: both
sides go through ``lazyportfolio.v2.store`` (``list_saved_models``,
``read_model``, ``write_model``, ``delete_model``) for persistence and
``lazyportfolio.v2.mode.mode_from_config`` for run-mode derivation, so a tree
built here shows up in the GUI and a tree built in the GUI loads here —
never a one-off export/import translation.

Imports ``lazyportfolio`` lazily (inside ``__init__``, not at module level)
so this module stays independent of the sibling ``tools.py``'s module-level
``lazyfin`` dependency.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from lazybridge import Tool

if TYPE_CHECKING:
    from lazyportfolio import OptimizationDataBackend

_TREE_SHAPE = (
    "A tree config is one JSON object: {root_id, nodes: [{id, name, children: "
    "[child_id,...], instruments: [tickers], proxy: ticker-or-'', goal: "
    "{objective}, constraints: {...}}], data: {start, end}, backtest: {id, "
    "train_size, estimation_frequency, rebalance_frequency, "
    "transaction_cost_bps, forward_enabled, hierarchy_mode, benchmark: {name, "
    "weights}}}. children is a list of OTHER node ids in the same nodes list "
    "(not nested objects) -- every node but the root needs exactly one parent "
    "and a proxy ticker; constraints may be {} (defaults fill in). This is "
    "the identical format Tree Studio (the local visual editor) saves -- "
    "call portfolio_tree_validate to see the exact error and required shape "
    "before saving."
)


def _json(payload: object) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _node_payload(results: dict[str, Any]) -> dict[str, Any]:
    """Project node results to weights + audit only -- never a return series."""
    return {
        name: {
            "local_weights": result.local_weights,
            "terminal_weights": result.terminal_weights,
            "audit": asdict(result.audit),
        }
        for name, result in results.items()
    }


class PortfolioTreeTools:
    """A ``ToolProvider`` over LazyPortfolio's tree store, mode derivation and engine.

    ``portfolio_tree_validate``/``_list``/``_load`` are pure reads, always
    emitted. ``allow_write`` additionally emits ``_save``/``_delete`` (persist
    to the shared store) and ``_estimate``/``_backtest`` (run the engine —
    gated like the rest of this connector's compute-cost writers, not because
    they mutate anything themselves).
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        allow_write: bool = False,
        backend: "OptimizationDataBackend | None" = None,
        store_dir: str | None = None,
    ) -> None:
        try:
            from lazyportfolio import (
                HierarchicalV2Backtester,
                HierarchicalV2Estimator,
                MarketDataHubOptimizationBackend,
                V2Model,
                delete_model,
                list_saved_models,
                mode_from_config,
                read_model,
                resolve_models_dir,
                write_model,
            )
            from lazyportfolio.calendar import _annualization_factor, _resample_simple_returns
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise ImportError(
                "PortfolioTreeTools requires the lazyportfolio package: "
                "pip install 'lazyportfolio @ git+https://github.com/selvaz/LazyPortfolio.git'"
            ) from exc
        self._allow_write = allow_write
        self._backend = backend
        self._store_dir = store_dir
        self._market_data_backend = MarketDataHubOptimizationBackend
        self._v2_model = V2Model
        self._estimator = HierarchicalV2Estimator()
        self._backtester = HierarchicalV2Backtester()
        self._mode_from_config = mode_from_config
        self._list_saved_models = list_saved_models
        self._read_model = read_model
        self._write_model = write_model
        self._delete_model = delete_model
        self._resolve_models_dir = resolve_models_dir
        self._annualization_factor = _annualization_factor
        self._resample_simple_returns = _resample_simple_returns

    def _resolve_backend(self) -> "OptimizationDataBackend":
        if self._backend is None:
            self._backend = self._market_data_backend()
        return self._backend

    # ------------------------------------------------------------------ #
    # ToolProvider
    # ------------------------------------------------------------------ #
    def as_tools(self) -> list[Tool]:
        tools = [
            Tool.wrap(
                self._validate,
                name="portfolio_tree_validate",
                description=(
                    "Check whether a tree config is a valid LazyPortfolio V2 model "
                    "(never touches market data or the saved-tree store) -- call "
                    "this before portfolio_tree_save to see a clear structural "
                    "error, or the flattened instrument universe and per-sleeve "
                    "breakdown when it's valid. " + _TREE_SHAPE
                ),
            ),
            Tool.wrap(
                self._list,
                name="portfolio_tree_list",
                description=(
                    "List saved tree configurations (name, filename, last-modified) "
                    "in the shared store -- the same directory Tree Studio (the "
                    "local visual editor) reads and writes, so this reflects trees "
                    "built there too. Response includes the resolved directory."
                ),
            ),
            Tool.wrap(
                self._load,
                name="portfolio_tree_load",
                description=(
                    "Load one saved tree config by name -- the raw JSON object, "
                    "unmodified since it was saved (by this tool or by Tree "
                    "Studio). Args: name."
                ),
            ),
        ]
        if self._allow_write:
            tools += [
                Tool.wrap(
                    self._save,
                    name="portfolio_tree_save",
                    description=(
                        "Validate and persist a tree config to the shared store, "
                        "under `name` -- it will immediately appear in Tree "
                        "Studio's saved-model list too. Refuses to write anything "
                        "if the config fails validation (same check as "
                        "portfolio_tree_validate). Args: name, config. " + _TREE_SHAPE
                    ),
                ),
                Tool.wrap(
                    self._delete,
                    name="portfolio_tree_delete",
                    description="Delete a saved tree config by name. Args: name.",
                ),
                Tool.wrap(
                    self._estimate,
                    name="portfolio_tree_estimate",
                    description=(
                        "Estimate target weights for a multi-node allocation tree "
                        "-- flat, forward, or forward-plus-backward, derived from "
                        "the tree's own backtest.forward_enabled/hierarchy_mode "
                        "(never chosen ad hoc). Pass EITHER `config` (inline) OR "
                        "`name` (a tree saved with portfolio_tree_save or built in "
                        "Tree Studio) -- config wins if both are given. Optional "
                        "estimation_frequency/train_size override the tree's own "
                        "values for this call only, without changing the saved "
                        "file. Returns terminal_weights and, per node, "
                        "local/terminal weights plus the solver audit -- never "
                        "raw return observations. " + _TREE_SHAPE
                    ),
                ),
                Tool.wrap(
                    self._backtest,
                    name="portfolio_tree_backtest",
                    description=(
                        "Causal walk-forward backtest of a multi-node allocation "
                        "tree, same mode derivation and config resolution as "
                        "portfolio_tree_estimate (`config` inline or `name` "
                        "saved/from Tree Studio; config wins if both given). "
                        "Optional estimation_frequency/train_size/"
                        "rebalance_frequency/transaction_cost_bps override the "
                        "tree's own values for this call only. Returns only "
                        "aggregate metrics and provenance, never return "
                        "observations or per-period curves. " + _TREE_SHAPE
                    ),
                ),
            ]
        return tools

    # ------------------------------------------------------------------ #
    # Store-backed tools
    # ------------------------------------------------------------------ #
    def _validate(self, config: dict[str, Any]) -> str:
        try:
            model = self._v2_model.from_config(config)
        except (KeyError, TypeError, ValueError) as exc:
            return _json({"ok": False, "error": str(exc)})
        instruments = list(
            dict.fromkeys(
                [
                    *model.root.terminal_instruments(),
                    *(node.proxy for node in model.root.walk() if node.proxy),
                    *model.benchmark.weights,
                ]
            )
        )
        return _json(
            {
                "ok": True,
                "instruments": instruments,
                "root_has_children": bool(model.root.children),
                "sleeves": [
                    {
                        "node": child.name,
                        "proxy": child.proxy,
                        "instruments": child.instruments,
                        "terminal_instruments": child.terminal_instruments(),
                    }
                    for child in model.root.children
                ],
            }
        )

    def _list(self) -> str:
        directory = self._resolve_models_dir(self._store_dir)
        return _json({"ok": True, "directory": str(directory), "items": self._list_saved_models(store_dir=self._store_dir)})

    def _load(self, name: str) -> str:
        return _json(self._read_model(name, store_dir=self._store_dir))

    def _save(self, name: str, config: dict[str, Any]) -> str:
        path = self._write_model(name, config, store_dir=self._store_dir)
        return _json({"ok": True, "name": path.stem, "path": str(path), "directory": str(path.parent)})

    def _delete(self, name: str) -> str:
        path = self._delete_model(name, store_dir=self._store_dir)
        return _json({"ok": True, "name": path.stem})

    # ------------------------------------------------------------------ #
    # Engine-backed tools
    # ------------------------------------------------------------------ #
    def _resolve_config(self, config: dict[str, Any] | None, name: str) -> dict[str, Any]:
        if config is not None:
            return config
        if name:
            return self._read_model(name, store_dir=self._store_dir)
        raise ValueError("either config or name must be given")

    @staticmethod
    def _effective_backtest(
        config: dict[str, Any],
        *,
        estimation_frequency: str,
        train_size: int,
        rebalance_frequency: str = "",
        transaction_cost_bps: float | None = None,
    ) -> dict[str, Any]:
        """The config's own backtest block, with any non-empty call-time
        override applied for this call only -- never written back to disk."""
        backtest = dict(config.get("backtest") or {})
        if estimation_frequency:
            backtest["estimation_frequency"] = estimation_frequency
        if train_size:
            backtest["train_size"] = train_size
        if rebalance_frequency:
            backtest["rebalance_frequency"] = rebalance_frequency
        if transaction_cost_bps is not None:
            backtest["transaction_cost_bps"] = transaction_cost_bps
        return backtest

    def _estimate(
        self,
        config: dict[str, Any] | None = None,
        name: str = "",
        estimation_frequency: str = "",
        train_size: int = 0,
    ) -> str:
        resolved = self._resolve_config(config, name)
        backtest = self._effective_backtest(
            resolved, estimation_frequency=estimation_frequency, train_size=train_size
        )
        merged = {**resolved, "backtest": backtest}
        model = self._v2_model.from_config(merged)
        mode = self._mode_from_config(merged)
        frequency = str(backtest.get("estimation_frequency") or "W")
        window = int(backtest.get("train_size") or 104)

        instruments = list(
            dict.fromkeys(
                [
                    *model.root.terminal_instruments(),
                    *(node.proxy for node in model.root.walk() if node.proxy),
                    *model.benchmark.weights,
                ]
            )
        )
        data = merged.get("data") if isinstance(merged.get("data"), dict) else {}
        dataset = self._resolve_backend().load_returns(
            instruments, start=str(data.get("start") or ""), end=str(data.get("end") or "")
        )
        estimation = self._resample_simple_returns(dataset.returns, frequency)
        train = estimation.tail(window)
        if len(train) < window:
            raise ValueError("not enough observations for the requested estimation window")
        estimate = self._estimator.estimate(
            model, train, mode=mode, periods_per_year=self._annualization_factor(frequency)
        )
        return _json(
            {
                "ok": True,
                "engine": "hierarchical-v2",
                "mode": mode,
                "terminal_weights": estimate.terminal_weights,
                "synthetic_benchmark_weights": estimate.synthetic_benchmark_weights,
                "nodes": _node_payload(estimate.node_results),
                "forward_nodes": _node_payload(estimate.forward_node_results),
                "provenance": {
                    "source": dataset.metadata.get("source"),
                    "n_rows": dataset.metadata.get("n_rows"),
                    "estimation_frequency": frequency,
                    "train_size": window,
                },
            }
        )

    def _backtest(
        self,
        config: dict[str, Any] | None = None,
        name: str = "",
        estimation_frequency: str = "",
        train_size: int = 0,
        rebalance_frequency: str = "",
        transaction_cost_bps: float | None = None,
    ) -> str:
        resolved = self._resolve_config(config, name)
        backtest = self._effective_backtest(
            resolved,
            estimation_frequency=estimation_frequency,
            train_size=train_size,
            rebalance_frequency=rebalance_frequency,
            transaction_cost_bps=transaction_cost_bps,
        )
        merged = {**resolved, "backtest": backtest}
        model = self._v2_model.from_config(merged)
        mode = self._mode_from_config(merged)
        frequency = str(backtest.get("estimation_frequency") or "W")

        instruments = list(
            dict.fromkeys(
                [
                    *model.root.terminal_instruments(),
                    *(node.proxy for node in model.root.walk() if node.proxy),
                    *model.benchmark.weights,
                ]
            )
        )
        data = merged.get("data") if isinstance(merged.get("data"), dict) else {}
        dataset = self._resolve_backend().load_returns(
            instruments, start=str(data.get("start") or ""), end=str(data.get("end") or "")
        )
        report = self._backtester.run(
            model,
            dataset.returns,
            mode=mode,
            train_size=int(backtest.get("train_size") or 104),
            estimation_frequency=frequency,
            rebalance_frequency=str(backtest.get("rebalance_frequency") or "M"),
            transaction_cost_bps=float(backtest.get("transaction_cost_bps") or 0),
        )
        return _json(
            {
                "ok": True,
                "engine": "hierarchical-v2",
                "mode": mode,
                "n_folds": len(report.folds),
                "metrics": report.metrics.get("FINAL"),
                "benchmark_metrics": report.metrics.get(model.benchmark.name),
                "transaction_cost_paid": report.transaction_cost_paid.get("FINAL"),
                "provenance": {
                    "source": dataset.metadata.get("source"),
                    "n_rows": dataset.metadata.get("n_rows"),
                    "estimation_frequency": frequency,
                    "rebalance_frequency": str(backtest.get("rebalance_frequency") or "M"),
                },
            }
        )


__all__ = ["PortfolioTreeTools"]
