"""Graph selector — query assets by id, tag, type, and graph traversal."""

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

# Matches patterns like: +name+2, +name, name+, name+3, +name+
_GRAPH_PATTERN = re.compile(
    r"^(?P<upstream>\+)?(?P<name>.+?)(?P<downstream>\+(?P<depth>\d+)?)?$"
)

logger = logging.getLogger(__name__)


class GraphSelector(Selector):
    """Parse and execute selector expressions against a Registry graph."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    @property
    def _graph(self) -> AssetGraph:
        return self._registry.graph

    def execute(self, selector: str) -> SelectionResult:
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
        """
        # Comma-separated = intersection (AND)
        parts = [p.strip() for p in selector.split(",")]
        if not any(parts):
            return SelectionResult(warnings=["Selector is empty."])

        if len(parts) > 1:
            result_sets = [self._execute_single(p) for p in parts]
            intersected = result_sets[0]
            for rs in result_sets[1:]:
                intersected = intersected & rs
            matched = [
                self._graph.assets[n]
                for n in sorted(intersected)
                if n in self._graph.assets
            ]
            return SelectionResult(assets=matched, names=intersected)

        return self._resolve(parts[0])

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


SelectorParser = GraphSelector
