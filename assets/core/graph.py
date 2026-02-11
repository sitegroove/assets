"""AssetGraph — directed acyclic graph of assets and dependencies."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from assets.core.asset import Asset
    from assets.core.dependency import Dependency


class SelectionResult(BaseModel):
    """Result of a selector query."""

    assets: list[Any] = []
    names: set[str] = set()


class AssetGraph:
    """Directed acyclic graph. Nodes = assets, edges = dependencies."""

    def __init__(self) -> None:
        self._assets: dict[str, Asset] = {}
        self._forward: dict[str, set[str]] = defaultdict(set)  # parent → children
        self._backward: dict[str, set[str]] = defaultdict(set)  # child → parents
        self._dependencies: list[Dependency] = []

    @classmethod
    def build(
        cls,
        assets: dict[str, Asset],
        dependencies: list[Dependency],
    ) -> AssetGraph:
        g = cls()
        g._assets = dict(assets)
        g._dependencies = list(dependencies)
        for dep in dependencies:
            g._forward[dep.source].add(dep.target)
            g._backward[dep.target].add(dep.source)
        return g

    # — Traversal —

    def ancestors(self, name: str, max_depth: int | None = None) -> set[str]:
        """All upstream assets (transitive parents)."""
        return self._traverse(name, self._backward, max_depth)

    def descendants(self, name: str, max_depth: int | None = None) -> set[str]:
        """All downstream assets (transitive children)."""
        return self._traverse(name, self._forward, max_depth)

    def roots(self) -> set[str]:
        """Assets with no upstream dependencies."""
        return {n for n in self._assets if not self._backward.get(n)}

    def leaves(self) -> set[str]:
        """Assets with no downstream dependents."""
        return {n for n in self._assets if not self._forward.get(n)}

    def topological_sort(self) -> list[str]:
        """Kahn's algorithm — returns assets in dependency order."""
        in_degree: dict[str, int] = {n: 0 for n in self._assets}
        for src, targets in self._forward.items():
            for t in targets:
                if t in in_degree:
                    in_degree[t] += 1

        queue = deque(sorted(n for n, d in in_degree.items() if d == 0))
        result: list[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for child in sorted(self._forward.get(node, set())):
                if child in in_degree:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        queue.append(child)

        if len(result) != len(self._assets):
            raise ValueError("Cycle detected in asset graph")
        return result

    # — Selection —

    def select(self, selector: str) -> SelectionResult:
        from assets.selector.parser import SelectorParser

        parser = SelectorParser(self)
        return parser.execute(selector)

    # — Properties —

    @property
    def fingerprint(self) -> str:
        """Hash of all assets + topology."""
        parts: list[str] = []
        for name in sorted(self._assets):
            parts.append(f"{name}:{self._assets[name].fingerprint}")
        for dep in sorted(self._dependencies, key=lambda d: (d.source, d.target)):
            parts.append(f"dep:{dep.source}:{dep.target}:{dep.type}")
        raw = json.dumps(parts, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def assets(self) -> dict[str, Asset]:
        return self._assets

    @property
    def dependencies(self) -> list[Dependency]:
        return self._dependencies

    def __len__(self) -> int:
        return len(self._assets)

    def __contains__(self, name: str) -> bool:
        return name in self._assets

    # — Internal —

    def _traverse(
        self,
        start: str,
        edges: dict[str, set[str]],
        max_depth: int | None = None,
    ) -> set[str]:
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        while queue:
            node, depth = queue.popleft()
            for neighbor in edges.get(node, set()):
                if neighbor not in visited:
                    if max_depth is not None and depth + 1 > max_depth:
                        continue
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
        return visited
