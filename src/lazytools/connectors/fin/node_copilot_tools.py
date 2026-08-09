"""Node Copilot's read-only tool profile over LazyPortfolio's copilot package.

docs/node-copilot-operational-plan.md §7.2: a strictly read-only surface —
node context, parent/children summaries, recent runs, view validation, and
counterfactual estimation. No save, no delete, no apply: this provider
never gets ``allow_persist``/``allow_delete`` (unlike ``PortfolioTreeTools``,
which supports both for other, fuller-privileged callers). Creating a
``ChangeProposal`` from a validated view set is the calling application
service's job (Fase 3+), not a tool on this provider.

Imports ``lazyportfolio`` lazily (inside ``__init__``), matching
``PortfolioTreeTools``'s own optional-dependency boundary.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from lazybridge import Tool

if TYPE_CHECKING:
    from lazyportfolio import OptimizationDataBackend

_VIEW_SHAPE = (
    "Each view: {instruments: {ticker: weight, ...}, expected_return: decimal "
    "(0.04 = 4%), confidence: number in (0,1], rationale: str}. instruments is "
    "a pick vector -- {TICKER: 1.0} is absolute, {A: 1.0, B: -1.0} is relative "
    "(A outperforms B). confidence must reflect actual evidence strength, "
    "never default to 1.0."
)


def _json(payload: object) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


class NodeCopilotReadTools:
    """A ``ToolProvider`` over ``lazyportfolio.copilot``'s node-scoped services.

    Every tool takes a ``tree_id`` (the UUID ``lazyportfolio.copilot.repository``
    assigns a tree, not its display name) and resolves against that tree's
    current head revision -- never a client-supplied config, so a stale or
    tampered inline config can never substitute for the tree's real state.
    """

    _is_lazy_tool_provider = True

    def __init__(
        self,
        *,
        backend: OptimizationDataBackend | None = None,
        store_path: str | None = None,
    ) -> None:
        try:
            from lazyportfolio.copilot import counterfactual as _counterfactual
            from lazyportfolio.copilot import node_universe as _node_universe
            from lazyportfolio.copilot import repository as _repository
            from lazyportfolio.copilot import snapshot as _snapshot
            from lazyportfolio.copilot.contracts import ProposedView
            from lazyportfolio.v2 import run_history as _run_history
            from lazyportfolio.v2.mode import mode_from_config
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise ImportError(
                "NodeCopilotReadTools requires the lazyportfolio package: "
                "pip install 'lazyportfolio @ git+https://github.com/selvaz/LazyPortfolio.git'"
            ) from exc
        # backend=None is a valid, meaningful value here (not "not yet
        # resolved"): it is forwarded as-is to
        # lazyportfolio.copilot.snapshot.load_snapshot, which falls back to
        # the real Market Data Hub backend itself -- this class never needs
        # its own copy of that fallback.
        self._backend = backend
        self._store_path = store_path
        self._get_head = _repository.get_head
        self._resolve_node_context = _node_universe.resolve_node_context
        self._find_node = _node_universe.find_node
        self._validate_view_set = _node_universe.validate_view_set
        self._evaluate_counterfactual = _counterfactual.evaluate_view_counterfactual
        self._load_snapshot = _snapshot.load_snapshot
        self._list_runs = _run_history.list_runs
        self._mode_from_config = mode_from_config
        self._proposed_view = ProposedView

    def as_tools(self) -> list[Tool]:
        return [
            Tool.wrap(
                self._get_node_context,
                name="tree_get_node_context",
                description=(
                    "Canonical, LazyPortfolio-resolved context for one node of a "
                    "tree at its CURRENT head revision: objective, mode, direct "
                    "instruments, children, allowed_view_instruments (the only "
                    "valid keys for a view on this node), current views, parent. "
                    "Args: tree_id, node_id."
                ),
            ),
            Tool.wrap(
                self._get_parent_context,
                name="tree_get_parent_context",
                description=(
                    "Same shape as tree_get_node_context, for node_id's parent -- "
                    "returns ok=false with no error if node_id is the root (roots "
                    "have no parent). Args: tree_id, node_id."
                ),
            ),
            Tool.wrap(
                self._get_child_summaries,
                name="tree_get_child_summaries",
                description=(
                    "Brief summaries (id, name, proxy, objective) of node_id's "
                    "direct children -- never their own nested children or "
                    "constraints; call tree_get_node_context on a specific child "
                    "id for that. Args: tree_id, node_id."
                ),
            ),
            Tool.wrap(
                self._get_revision,
                name="tree_get_revision",
                description=(
                    "This tree's current head revision: revision_id, "
                    "parent_revision_id, config_hash, created_at, actor_type, "
                    "actor_id, reason. Historical (non-head) revisions are not "
                    "queryable through this tool yet. Args: tree_id."
                ),
            ),
            Tool.wrap(
                self._get_recent_runs,
                name="tree_get_recent_runs",
                description=(
                    "Recent estimate/backtest runs recorded for a SAVED tree, by "
                    "its display name (not tree_id -- run history is still keyed "
                    "by name, reconciled with tree_id in a later phase). Args: "
                    "name, limit (default 10)."
                ),
            ),
            Tool.wrap(
                self._validate_views,
                name="portfolio_tree_validate_views",
                description=(
                    "Validate a candidate view set against node_id's resolved "
                    "universe: rejects instruments outside "
                    "allowed_view_instruments, financing instruments, "
                    "non-finite/all-zero coefficients, duplicate or exact-opposite "
                    "picks; warns (does not reject) on extreme expected_return or "
                    "when the node's objective/view_covariance_policy combination "
                    "makes views a no-op. Never touches the saved tree. Args: "
                    "tree_id, node_id, views. " + _VIEW_SHAPE
                ),
            ),
            Tool.wrap(
                self._estimate_counterfactual,
                name="portfolio_tree_estimate_counterfactual",
                description=(
                    "Solve node_id's tree twice on the SAME loaded dataset -- once "
                    "as saved, once with `views` substituted for node_id's own "
                    "views -- and return the diff (terminal/local weight deltas, "
                    "one-way turnover). This is current_estimate_counterfactual "
                    "(§6.4): causally correct against the CURRENT snapshot, never "
                    "a backtest and never a claim about historical performance. "
                    "Loads real market data -- has real compute cost. Args: "
                    "tree_id, node_id, views, periods_per_year (default 252). "
                    + _VIEW_SHAPE
                ),
            ),
        ]

    # ------------------------------------------------------------------ #
    # Context reads
    # ------------------------------------------------------------------ #
    def _load_head(self, tree_id: str) -> Any:
        head = self._get_head(tree_id, db_path=self._store_path)
        if head is None:
            raise ValueError(f"tree {tree_id!r} has no revisions yet")
        return head

    def _get_node_context(self, tree_id: str, node_id: str) -> str:
        head = self._load_head(tree_id)
        mode = self._mode_from_config(head.config)
        context = self._resolve_node_context(
            head.config,
            node_id,
            mode=mode,
            tree_id=head.tree_id,
            revision_id=head.revision_id,
        )
        return _json({"ok": True, "context": context.model_dump(mode="json")})

    def _get_parent_context(self, tree_id: str, node_id: str) -> str:
        head = self._load_head(tree_id)
        mode = self._mode_from_config(head.config)
        context = self._resolve_node_context(
            head.config, node_id, mode=mode, tree_id=head.tree_id, revision_id=head.revision_id
        )
        if context.parent_node_id is None:
            return _json({"ok": False, "error": f"node {node_id!r} is the root; it has no parent"})
        parent_context = self._resolve_node_context(
            head.config,
            context.parent_node_id,
            mode=mode,
            tree_id=head.tree_id,
            revision_id=head.revision_id,
        )
        return _json({"ok": True, "context": parent_context.model_dump(mode="json")})

    def _get_child_summaries(self, tree_id: str, node_id: str) -> str:
        head = self._load_head(tree_id)
        from lazyportfolio.v2.model import V2Model

        model = V2Model.from_config(head.config)
        node = self._find_node(model, node_id)
        return _json(
            {
                "ok": True,
                "children": [
                    {
                        "id": child.id,
                        "name": child.name,
                        "proxy": child.proxy,
                        "objective": child.objective,
                    }
                    for child in node.children
                ],
            }
        )

    def _get_revision(self, tree_id: str) -> str:
        head = self._load_head(tree_id)
        return _json(
            {
                "ok": True,
                "revision_id": head.revision_id,
                "parent_revision_id": head.parent_revision_id,
                "config_hash": head.config_hash,
                "created_at": head.created_at,
                "actor_type": head.actor_type,
                "actor_id": head.actor_id,
                "reason": head.reason,
            }
        )

    def _get_recent_runs(self, name: str, limit: int = 10) -> str:
        runs = self._list_runs(tree_id=name, limit=limit, db_path=self._store_path)
        return _json(
            {
                "ok": True,
                "runs": [
                    {
                        "id": run["id"],
                        "kind": run["kind"],
                        "created_at": run["created_at"],
                        "data_as_of": run["data_as_of"],
                        "metrics": run["metrics"],
                    }
                    for run in runs
                ],
            }
        )

    # ------------------------------------------------------------------ #
    # Validation / counterfactual
    # ------------------------------------------------------------------ #
    def _parse_views(self, views: list[dict[str, Any]]) -> list[Any]:
        return [self._proposed_view(**view) for view in views]

    def _validate_views(
        self, tree_id: str, node_id: str, views: list[dict[str, Any]]
    ) -> str:
        head = self._load_head(tree_id)
        mode = self._mode_from_config(head.config)
        result = self._validate_view_set(
            head.config, node_id, self._parse_views(views), mode=mode
        )
        return _json({"ok": True, "validation": result.model_dump(mode="json")})

    def _estimate_counterfactual(
        self,
        tree_id: str,
        node_id: str,
        views: list[dict[str, Any]],
        periods_per_year: float = 252.0,
    ) -> str:
        head = self._load_head(tree_id)
        mode = self._mode_from_config(head.config)
        _, dataset, _ = self._load_snapshot(head.config, backend=self._backend)
        result = self._evaluate_counterfactual(
            head.config,
            node_id,
            self._parse_views(views),
            dataset,
            mode=mode,
            periods_per_year=periods_per_year,
        )
        return _json({"ok": True, "counterfactual": result.model_dump(mode="json")})


__all__ = ["NodeCopilotReadTools"]
