"""AssetGraph — directed acyclic graph of assets and dependencies.

Thread-safe: the graph is structurally frozen after ``build()``.
Lazy caches (``fingerprint``, ``topological_sort``) are guarded
by a lock so that concurrent readers don't race on cache population.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from assets.core.asset import Asset
    from assets.core.dependency import Dependency


@dataclass
class SelectionResult:
    """Result of a selector query."""

    assets: list[Asset] = field(default_factory=list)
    names: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


class AssetGraph:
    """Directed acyclic graph. Nodes = assets, edges = dependencies."""

    def __init__(self) -> None:
        self._assets: dict[str, Asset] = {}
        self._forward: dict[str, set[str]] = defaultdict(set)  # parent → children
        self._backward: dict[str, set[str]] = defaultdict(set)  # child → parents
        self._dependencies: list[Dependency] = []
        # Lock for lazy cache population (fingerprint, topo sort)
        self._cache_lock = threading.Lock()
        self._fingerprint_cache: str | None = None
        self._topo_cache: list[str] | None = None
        # Secondary indexes for O(1) selector lookups (built eagerly)
        self._tag_index: dict[str, set[str]] = defaultdict(set)
        self._type_index: dict[str, set[str]] = defaultdict(set)

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
        # Build secondary indexes
        for name, asset in g._assets.items():
            for tag in getattr(asset, "tags", []):
                g._tag_index[tag].add(name)
            asset_type = getattr(asset, "type", "")
            if asset_type:
                g._type_index[asset_type].add(name)
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
        """Kahn's algorithm — returns assets in dependency order (cached).

        Thread-safe: the cache is populated under a lock so concurrent
        callers don't duplicate work.
        """
        with self._cache_lock:
            if self._topo_cache is not None:
                return list(self._topo_cache)

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
            self._topo_cache = result
            return list(result)

    # — Index lookups —

    def names_by_tag(self, tag: str) -> set[str]:
        """Return asset names matching a tag (O(1) via index)."""
        return set(self._tag_index.get(tag, set()))

    def names_by_type(self, type_val: str) -> set[str]:
        """Return asset names matching a type (O(1) via index)."""
        return set(self._type_index.get(type_val, set()))

    # — Staleness —

    def stale(self, changed: set[str]) -> set[str]:
        """Return *changed* names plus all transitive downstream dependents.

        Safe for names not in the graph (e.g. deleted assets that were
        already unregistered) — they appear in the result but do not
        trigger traversal.
        """
        result = set(changed)
        for name in changed:
            if name in self._assets:
                result |= self.descendants(name)
        return result

    # — Properties —

    @property
    def fingerprint(self) -> str:
        """Hash of all assets + topology. Cached after first computation.

        Thread-safe: the cache is populated under a lock.
        """
        with self._cache_lock:
            if self._fingerprint_cache is None:
                parts: list[str] = []
                for name in sorted(self._assets):
                    parts.append(f"{name}:{self._assets[name].fingerprint}")
                for dep in sorted(
                    self._dependencies, key=lambda d: (d.source, d.target)
                ):
                    parts.append(f"dep:{dep.source}:{dep.target}:{dep.type}")
                raw = json.dumps(parts, sort_keys=True)
                self._fingerprint_cache = hashlib.sha256(raw.encode()).hexdigest()
            return self._fingerprint_cache

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

    def __repr__(self) -> str:
        return f"AssetGraph(assets={len(self._assets)}, deps={len(self._dependencies)})"

    def __getitem__(self, name: str) -> Asset:
        """Lookup an asset by name with a clear error message."""
        try:
            return self._assets[name]
        except KeyError:
            available = sorted(self._assets.keys())[:5]
            suffix = "..." if len(self._assets) > 5 else ""
            raise KeyError(
                f"Asset '{name}' not found in graph "
                f"({len(self._assets)} assets). "
                f"Available: {available}{suffix}"
            ) from None

    # — Export utilities —

    def to_dict(self) -> dict[str, Any]:
        """Export the graph as a serializable dict.

        Returns a dict with ``assets`` (list of dicts) and
        ``dependencies`` (list of edge dicts).
        """
        return {
            "assets": [
                a.model_dump()
                for a in sorted(self._assets.values(), key=lambda a: a.id)
            ],
            "dependencies": [
                {"source": d.source, "target": d.target, "type": d.type}
                for d in sorted(self._dependencies, key=lambda d: (d.source, d.target))
            ],
        }

    def to_mermaid(self) -> str:
        """Export the graph as a Mermaid flowchart.

        Returns a string that can be pasted into a Mermaid-compatible
        renderer (GitHub markdown, documentation sites, etc.).

        Example output::

            graph LR
                raw_users["raw.users"]
                staging_users["staging.users"]
                raw_users --> staging_users
        """
        lines: list[str] = ["graph LR"]

        def _safe_id(name: str) -> str:
            return name.replace(".", "_").replace("-", "_").replace(" ", "_")

        for name in sorted(self._assets):
            lines.append(f'    {_safe_id(name)}["{name}"]')

        for dep in sorted(self._dependencies, key=lambda d: (d.source, d.target)):
            lines.append(f"    {_safe_id(dep.source)} --> {_safe_id(dep.target)}")

        return "\n".join(lines)

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
