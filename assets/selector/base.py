"""Selector base class with optional state awareness."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from assets.core.graph import SelectionResult

if TYPE_CHECKING:
    from assets.core.registry import Registry
    from assets.engine.manager import StateManager
    from assets.engine.planner import Plan

_VALID_STATE_VALUES = frozenset({"modified", "created", "updated", "deleted"})


class Selector(ABC):
    """Abstract selector interface with built-in state awareness.

    Subclasses implement :meth:`execute` to define their query syntax.
    State-based selection helpers (``_resolve_plan``, ``_state_names``)
    are available to any subclass that receives a
    :class:`~assets.engine.manager.StateManager` at construction time.
    """

    def __init__(
        self,
        registry: Registry,
        *,
        manager: StateManager | None = None,
    ) -> None:
        self._registry = registry
        self._manager = manager

    @abstractmethod
    def execute(
        self,
        selector: str,
        *,
        environment: str | None = None,
    ) -> SelectionResult:
        """Execute a selector expression and return matching assets."""

    # ── State helpers available to all subclasses ─────────────

    def _resolve_plan(self, environment: str | None = None) -> Plan | None:
        """Build a plan for state-based selection.

        Returns ``None`` when no :class:`StateManager` is configured.
        """
        if self._manager is None:
            return None
        return self._manager.plan(environment=environment)

    def _state_names(
        self,
        state_value: str,
        plan: Plan | None,
    ) -> tuple[set[str], list[str]]:
        """Resolve a ``state:<value>`` term to a set of asset names.

        Args:
            state_value: One of ``modified``, ``created``, ``updated``,
                ``deleted``.
            plan: The plan to read change sets from, or ``None``.

        Returns:
            Tuple of (matched names, warnings).
        """
        warnings: list[str] = []

        if not state_value:
            warnings.append("State selector has an empty value.")
            return set(), warnings

        if state_value not in _VALID_STATE_VALUES:
            valid = ", ".join(sorted(_VALID_STATE_VALUES))
            warnings.append(
                f"Unknown state selector '{state_value}'. Use one of: {valid}."
            )
            return set(), warnings

        if plan is None:
            return set(), warnings

        if state_value == "modified":
            return plan.changed_ids, warnings
        if state_value == "created":
            return plan.created_ids, warnings
        if state_value == "updated":
            return plan.updated_ids, warnings
        return plan.deleted_ids, warnings
