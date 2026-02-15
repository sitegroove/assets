"""Graph selector — query assets by id, tag, type, state, and graph traversal."""

from __future__ import annotations

import fnmatch
import logging
import re
from typing import TYPE_CHECKING

from assets.core.graph import SelectionResult
from assets.selector.base import Selector

if TYPE_CHECKING:
    from assets.core.graph import AssetGraph
    from assets.core.registry import Registry
    from assets.engine.manager import StateManager
    from assets.engine.planner import Plan

# Matches patterns like: +name+2, +name, name+, name+3, +name+
_GRAPH_PATTERN = re.compile(
    r"^(?P<upstream>\+)?(?P<name>.+?)(?P<downstream>\+(?P<depth>\d+)?)?$"
)

logger = logging.getLogger(__name__)


class GraphSelector(Selector):
    """Parse and execute selector expressions against a Registry graph.

    Handles graph-based selectors (tag, type, wildcard, traversal)
    and state-based selectors (``state:modified``, ``state:created``, etc.)
    in a single unified interface.

    State selectors require a :class:`~assets.engine.manager.StateManager`
    to be passed at construction time. When no manager is provided,
    ``state:`` terms return an empty result with a warning.
    """

    def __init__(
        self,
        registry: Registry,
        *,
        manager: StateManager | None = None,
    ) -> None:
        super().__init__(registry, manager=manager)

    @property
    def _graph(self) -> AssetGraph:
        return self._registry.graph

    def execute(
        self,
        selector: str,
        *,
        exclude: str | None = None,
        environment: str | None = None,
    ) -> SelectionResult:
        """Parse a selector string and return matching assets.

        Supported syntax:
            staging.users       — exact match
            tag:pii             — all with tag "pii"
            type:data_model     — all with type "data_model"
            +staging.users      — asset + all ancestors
            staging.users+      — asset + all descendants
            +staging.users+     — asset + ancestors + descendants
            raw.*               — wildcard name match
            tag:pii,type:data_model — intersection (AND)
            staging.users+2     — descendants up to depth 2
            state:modified      — assets changed since last apply
            state:created       — newly created assets
            state:updated       — updated assets
            state:deleted       — deleted assets
            state:modified+     — modified + their descendants
            +state:modified     — modified + their ancestors
            state:modified,tag:pii — intersection of modified AND tagged pii

        The ``exclude`` parameter accepts the same selector syntax.
        Matches from the exclude expression are subtracted from the
        main result (like dbt ``--exclude``).
        """
        result = self._execute_select(selector, environment=environment)

        if exclude:
            exclude_result = self._execute_select(exclude, environment=environment)
            result = self._subtract(result, exclude_result)

        return result

    # ── Internal dispatch ────────────────────────────────────

    def _execute_select(
        self,
        selector: str,
        *,
        environment: str | None = None,
    ) -> SelectionResult:
        """Resolve a selector expression to a SelectionResult."""
        parts = [p.strip() for p in selector.split(",")]
        if not any(parts):
            return SelectionResult(warnings=["Selector is empty."])

        has_state = any(self._is_state_term(p) for p in parts if p)

        if len(parts) > 1 or has_state:
            return self._execute_multi(parts, environment=environment)

        return self._resolve(parts[0])

    @staticmethod
    def _subtract(
        result: SelectionResult,
        exclude: SelectionResult,
    ) -> SelectionResult:
        """Subtract exclude matches from result."""
        remaining = result.names - exclude.names
        assets = [a for a in result.assets if a.id in remaining]
        return SelectionResult(
            assets=assets,
            names=remaining,
            warnings=result.warnings + exclude.warnings,
        )

    # ── Internal dispatch ────────────────────────────────────

    def _execute_multi(
        self,
        parts: list[str],
        *,
        environment: str | None = None,
    ) -> SelectionResult:
        """Handle multi-term (intersection) and state-aware selectors."""
        warnings: list[str] = []
        plan: Plan | None = None
        has_state = any(self._is_state_term(p) for p in parts if p)

        if has_state:
            if self._manager is None:
                warnings.append("State selector requires a StateManager.")
            else:
                plan = self._resolve_plan(environment)

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
                result = self._resolve(part)
                warnings.extend(result.warnings)
                result_sets.append(result.names)

        intersected = result_sets[0]
        for names in result_sets[1:]:
            intersected = intersected & names

        matched = [
            self._graph.assets[n]
            for n in sorted(intersected)
            if n in self._graph.assets
        ]
        return SelectionResult(
            assets=matched,
            names=intersected,
            warnings=warnings,
        )

    def _execute_single(self, selector: str) -> set[str]:
        """Execute a single selector term, return matching names."""
        return self._resolve(selector).names

    def _resolve(self, selector: str) -> SelectionResult:
        selector = selector.strip()
        if not selector:
            return SelectionResult(warnings=["Selector is empty."])

        # tag:X
        if selector.startswith("tag:"):
            tag = selector[4:]
            warnings: list[str] = []
            if not tag:
                warnings.append("Tag selector has an empty value.")
            names = self._graph.names_by_tag(tag)
            matched = [self._graph.assets[n] for n in sorted(names)]
            return SelectionResult(assets=matched, names=names, warnings=warnings)

        # type:X
        if selector.startswith("type:"):
            type_val = selector[5:]
            warnings: list[str] = []
            if not type_val:
                warnings.append("Type selector has an empty value.")
            names = self._graph.names_by_type(type_val)
            matched = [self._graph.assets[n] for n in sorted(names)]
            return SelectionResult(assets=matched, names=names, warnings=warnings)

        # Graph traversal patterns: +name, name+, +name+, name+2
        m = _GRAPH_PATTERN.match(selector)
        if not m:
            return SelectionResult(warnings=[f"Invalid selector syntax: '{selector}'."])

        name_part = m.group("name")
        upstream = m.group("upstream") is not None
        downstream = m.group("downstream") is not None and m.group("downstream") != ""
        depth_str = m.group("depth")
        max_depth = int(depth_str) if depth_str else None
        warnings: list[str] = []

        if "+" in name_part:
            warnings.append(
                "Selector has an unexpected '+' in asset name. "
                "Use graph traversal as '+asset', 'asset+', or 'asset+N'."
            )
        if selector.startswith("+") and len(selector) > 1 and selector[1].isdigit():
            warnings.append(
                "Selector starts with '+<digit>'. Did you mean '+asset' or 'asset+N'?"
            )

        # Resolve name_part (could be wildcard)
        base_names = self._match_names(name_part)
        if not base_names:
            if warnings:
                for warning in warnings:
                    logger.warning(
                        "Selector warning: %s selector=%r", warning, selector
                    )
            return SelectionResult(warnings=warnings)

        # Expand graph traversal
        result_names: set[str] = set(base_names)
        for base in base_names:
            if upstream:
                result_names |= self._graph.ancestors(base, max_depth)
            if downstream:
                result_names |= self._graph.descendants(base, max_depth)

        matched = [
            self._graph.assets[n]
            for n in sorted(result_names)
            if n in self._graph.assets
        ]
        if warnings:
            for warning in warnings:
                logger.warning("Selector warning: %s selector=%r", warning, selector)
        return SelectionResult(assets=matched, names=result_names, warnings=warnings)

    def _match_names(self, pattern: str) -> set[str]:
        """Match asset names by exact match or wildcard."""
        if "*" in pattern or "?" in pattern:
            return {n for n in self._graph.assets if fnmatch.fnmatch(n, pattern)}
        if pattern in self._graph.assets:
            return {pattern}
        return set()

    # ── State term helpers ───────────────────────────────────

    @staticmethod
    def _is_state_term(selector: str) -> bool:
        """Return True when a selector term targets ``state:<value>``."""
        match = _GRAPH_PATTERN.match(selector.strip())
        if not match:
            return False
        return match.group("name").startswith("state:")

    def _resolve_state_term(
        self,
        selector: str,
        plan: Plan | None,
    ) -> tuple[set[str], list[str]]:
        """Resolve a ``state:<value>`` term with optional graph expansion."""
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

        state_value = name_part[6:]  # strip "state:" prefix

        if "+" in state_value:
            warnings.append(
                "State selector has an unexpected '+' in state name. "
                "Use graph traversal as '+state:modified', "
                "'state:modified+', or 'state:modified+N'."
            )

        # Delegate to base class helper for validation and name resolution
        base_names, state_warnings = self._state_names(state_value, plan)
        warnings.extend(state_warnings)

        if not base_names:
            return set(), warnings

        # Expand graph traversal
        graph = self._registry.graph
        result_names = set(base_names)
        for base in base_names:
            if upstream:
                result_names |= graph.ancestors(base, max_depth)
            if downstream:
                result_names |= graph.descendants(base, max_depth)

        return result_names, warnings


SelectorParser = GraphSelector
