"""Selector parser — query assets by name, tag, kind, and graph traversal."""

from __future__ import annotations

import fnmatch
import re
from typing import TYPE_CHECKING

from assets.core.graph import SelectionResult

if TYPE_CHECKING:
    from assets.core.graph import AssetGraph

# Matches patterns like: +name+2, +name, name+, name+3, +name+
_GRAPH_PATTERN = re.compile(
    r"^(?P<upstream>\+)?(?P<name>.+?)(?P<downstream>\+(?P<depth>\d+)?)?$"
)


class SelectorParser:
    """Parse and execute selector expressions against an AssetGraph."""

    def __init__(self, graph: AssetGraph) -> None:
        self._graph = graph

    def execute(self, selector: str) -> SelectionResult:
        """Parse a selector string and return matching assets.

        Supported syntax:
            staging.users       — exact match
            tag:pii             — all with tag "pii"
            kind:data_model     — all with kind "data_model"
            +staging.users      — asset + all ancestors
            staging.users+      — asset + all descendants
            +staging.users+     — asset + ancestors + descendants
            raw.*               — wildcard name match
            tag:pii,kind:data_model — intersection (AND)
            staging.users+2     — descendants up to depth 2
        """
        # Comma-separated = intersection (AND)
        parts = [p.strip() for p in selector.split(",")]
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
        # tag:X
        if selector.startswith("tag:"):
            tag = selector[4:]
            names = {
                n for n, a in self._graph.assets.items() if tag in getattr(a, "tags", [])
            }
            matched = [self._graph.assets[n] for n in sorted(names)]
            return SelectionResult(assets=matched, names=names)

        # kind:X
        if selector.startswith("kind:"):
            kind = selector[5:]
            names = {
                n for n, a in self._graph.assets.items() if getattr(a, "kind", "") == kind
            }
            matched = [self._graph.assets[n] for n in sorted(names)]
            return SelectionResult(assets=matched, names=names)

        # Graph traversal patterns: +name, name+, +name+, name+2
        m = _GRAPH_PATTERN.match(selector)
        if not m:
            return SelectionResult()

        name_part = m.group("name")
        upstream = m.group("upstream") is not None
        downstream = m.group("downstream") is not None and m.group("downstream") != ""
        depth_str = m.group("depth")
        max_depth = int(depth_str) if depth_str else None

        # Resolve name_part (could be wildcard)
        base_names = self._match_names(name_part)
        if not base_names:
            return SelectionResult()

        # Expand graph traversal
        result_names: set[str] = set(base_names)
        for base in base_names:
            if upstream:
                result_names |= self._graph.ancestors(base, max_depth)
            if downstream:
                result_names |= self._graph.descendants(base, max_depth)

        matched = [
            self._graph.assets[n] for n in sorted(result_names) if n in self._graph.assets
        ]
        return SelectionResult(assets=matched, names=result_names)

    def _match_names(self, pattern: str) -> set[str]:
        """Match asset names by exact match or wildcard."""
        if "*" in pattern or "?" in pattern:
            return {n for n in self._graph.assets if fnmatch.fnmatch(n, pattern)}
        if pattern in self._graph.assets:
            return {pattern}
        return set()
