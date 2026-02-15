"""State-aware selectors built on top of graph selectors."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from assets.core.graph import SelectionResult
from assets.selector.base import Selector
from assets.selector.parser import GraphSelector

if TYPE_CHECKING:
    from assets.core.registry import Registry
    from assets.engine.manager import StateManager
    from assets.engine.planner import Plan

# Matches patterns like: +name+2, +name, name+, name+3, +name+
_GRAPH_PATTERN = re.compile(
    r"^(?P<upstream>\+)?(?P<name>.+?)(?P<downstream>\+(?P<depth>\d+)?)?$"
)

_VALID_STATE_SELECTORS = {
    "modified",
    "created",
    "updated",
    "deleted",
}


class StateSelector(Selector):
    """Execute selectors that can include ``state:`` terms."""

    def __init__(
        self,
        registry: Registry,
        manager: StateManager | None = None,
    ) -> None:
        self._registry = registry
        self._manager = manager
        self._graph_selector = GraphSelector(registry)

    def execute(
        self,
        selector: str,
        *,
        environment: str | None = None,
    ) -> SelectionResult:
        """Execute a selector expression, resolving ``state:`` terms via plan()."""
        parts = [p.strip() for p in selector.split(",")]
        if not any(parts):
            return SelectionResult(warnings=["Selector is empty."])

        if not any(self._is_state_term(part) for part in parts if part):
            return self._graph_selector.execute(selector)

        warnings: list[str] = []
        plan: Plan | None = None
        if self._manager is None:
            warnings.append("State selector requires a StateManager.")
        else:
            plan = self._manager.plan(environment=environment)

        result_sets: list[set[str]] = []
        for part in parts:
            if not part:
                warnings.append("Selector is empty.")
                result_sets.append(set())
                continue

            if self._is_state_term(part):
                names, part_warnings = self._resolve_state_term(part, plan)
                warnings.extend(part_warnings)
                result_sets.append(names)
            else:
                result = self._graph_selector.execute(part)
                warnings.extend(result.warnings)
                result_sets.append(result.names)

        intersected = result_sets[0]
        for names in result_sets[1:]:
            intersected = intersected & names

        assets = self._registry.graph.assets
        matched = [assets[n] for n in sorted(intersected) if n in assets]
        return SelectionResult(assets=matched, names=intersected, warnings=warnings)

    def _is_state_term(self, selector: str) -> bool:
        """Return True when selector term targets ``state:<value>``."""
        match = _GRAPH_PATTERN.match(selector.strip())
        if not match:
            return False
        return match.group("name").startswith("state:")

    def _resolve_state_term(
        self,
        selector: str,
        plan: Plan | None,
    ) -> tuple[set[str], list[str]]:
        warnings: list[str] = []
        match = _GRAPH_PATTERN.match(selector)
        if not match:
            return set(), [f"Invalid selector syntax: '{selector}'."]

        name_part = match.group("name")
        upstream = match.group("upstream") is not None
        downstream = (
            match.group("downstream") is not None and match.group("downstream") != ""
        )
        depth_str = match.group("depth")
        max_depth = int(depth_str) if depth_str else None

        state_value = name_part[6:]
        if not state_value:
            warnings.append("State selector has an empty value.")
            return set(), warnings

        if "+" in state_value:
            warnings.append(
                "State selector has an unexpected '+' in state name. "
                "Use graph traversal as '+state:modified', "
                "'state:modified+', or 'state:modified+N'."
            )

        if state_value not in _VALID_STATE_SELECTORS:
            valid = ", ".join(sorted(_VALID_STATE_SELECTORS))
            warnings.append(
                f"Unknown state selector '{state_value}'. Use one of: {valid}."
            )
            return set(), warnings

        if plan is None:
            return set(), warnings

        base_names: set[str]
        if state_value == "modified":
            base_names = plan.changed_ids
        elif state_value == "created":
            base_names = plan.created_ids
        elif state_value == "updated":
            base_names = plan.updated_ids
        else:
            base_names = plan.deleted_ids

        if not base_names:
            return set(), warnings

        graph = self._registry.graph
        result_names = set(base_names)
        for base in base_names:
            if upstream:
                result_names |= graph.ancestors(base, max_depth)
            if downstream:
                result_names |= graph.descendants(base, max_depth)

        return result_names, warnings
