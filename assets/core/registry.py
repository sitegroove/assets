"""Registry — central store of assets and dependencies.

Thread-safe: all mutations and the lazy graph property are guarded
by an ``RLock``.  ``RLock`` (not ``Lock``) is used because public
methods like ``register_many`` call internal helpers that also need
the lock, and ``select`` / ``resolve_field_dependency`` read the
graph while holding the lock.

Read-only methods (``get``, ``all``, ``__len__``, ``__contains__``)
acquire the same lock to guarantee a consistent snapshot.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from assets.core.dependency import Dependency, FieldMapping
from assets.core.graph import AssetGraph, SelectionResult

if TYPE_CHECKING:
    from assets.core.asset import Asset
    from assets.resolver.lineage import DependencyResolver


class Registry:
    """Central store of assets and dependencies with lazy graph construction.

    Consumers are responsible for setting dependencies on each asset
    before calling ``register()``. The library never inspects SQL content.

    All public methods are thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._assets: dict[str, Asset] = {}
        self._dependencies: list[Dependency] = []
        self._graph: AssetGraph | None = None

    def register(self, asset: Asset) -> None:
        """Register an asset and build dependency edges from ``depends_on``."""
        with self._lock:
            self._assets[asset.id] = asset
            self._graph = None  # invalidate cached graph
            # Remove old deps for this asset to avoid duplication on re-register
            self._dependencies = [d for d in self._dependencies if d.target != asset.id]
            for dep_name in dict.fromkeys(asset.depends_on):
                self._dependencies.append(
                    Dependency(source=dep_name, target=asset.id, type="ref")
                )

    def get(self, name: str) -> Asset | None:
        with self._lock:
            return self._assets.get(name)

    def all(self) -> list[Asset]:
        with self._lock:
            return list(self._assets.values())

    def clear(self) -> None:
        with self._lock:
            self._assets.clear()
            self._dependencies.clear()
            self._graph = None

    @property
    def graph(self) -> AssetGraph:
        """Lazily build DAG from current assets + dependencies."""
        with self._lock:
            if self._graph is None:
                self._graph = AssetGraph.build(self._assets, self._dependencies)
            return self._graph

    def select(self, selector: str) -> SelectionResult:
        """Query assets by selector expression."""
        return self.graph.select(selector)

    def resolve_field_dependency(
        self,
        asset_id: str | None = None,
        selector: str | None = None,
        resolver: DependencyResolver | None = None,
    ) -> list[FieldMapping]:
        """Resolve field-level dependencies using a consumer-provided resolver.

        The resolver must implement a ``resolve(sql, schema)`` method.
        This is intentionally generic — the library provides the base class,
        consumers bring their own implementation (e.g., sqlglot-based).

        SQL is passed to the resolver as-is. Consumers are responsible for
        resolving any template syntax (e.g., ``{{ ref() }}``) before
        registering assets.
        """
        if resolver is None:
            raise ValueError("A resolver instance must be provided by the consumer")

        targets: list[Asset] = []
        if asset_id:
            asset = self.get(asset_id)
            if asset:
                targets = [asset]
        elif selector:
            result = self.select(selector)
            targets = result.assets
        else:
            raise ValueError("Provide either asset_id or selector")

        mappings: list[FieldMapping] = []
        for asset in targets:
            if not asset.sql:
                continue
            schema: dict[str, list[str]] = {}
            for dep_name in asset.depends_on:
                dep_asset = self.get(dep_name)
                if dep_asset:
                    schema[dep_name] = dep_asset.list_children()
            result = resolver.resolve(asset.sql, schema)
            mappings.extend(result)
        return mappings

    def register_many(self, assets: list[Asset]) -> None:
        """Batch-register assets, invalidating the graph once."""
        with self._lock:
            target_names = {a.id for a in assets}
            self._dependencies = [
                d for d in self._dependencies if d.target not in target_names
            ]
            for asset in assets:
                self._assets[asset.id] = asset
                for dep_name in dict.fromkeys(asset.depends_on):
                    self._dependencies.append(
                        Dependency(source=dep_name, target=asset.id, type="ref")
                    )
            self._graph = None

    def unregister(self, name: str) -> bool:
        """Remove an asset and its dependency edges. Returns True if found."""
        with self._lock:
            if name not in self._assets:
                return False
            del self._assets[name]
            self._dependencies = [
                d for d in self._dependencies if d.source != name and d.target != name
            ]
            self._graph = None
            return True

    def unregister_many(self, names: list[str]) -> int:
        """Remove multiple assets and their dependency edges.

        Returns the count of assets actually removed.
        """
        with self._lock:
            name_set = set(names)
            removed = sum(1 for n in name_set if n in self._assets)
            for n in name_set:
                self._assets.pop(n, None)
            self._dependencies = [
                d
                for d in self._dependencies
                if d.source not in name_set and d.target not in name_set
            ]
            self._graph = None
            return removed

    def to_dict(self) -> dict[str, Any]:
        """Export the full catalog as a serializable dict."""
        return self.graph.to_dict()

    def to_mermaid(self) -> str:
        """Export the asset graph as a Mermaid flowchart."""
        return self.graph.to_mermaid()

    def __repr__(self) -> str:
        with self._lock:
            n_assets = len(self._assets)
            n_deps = len(self._dependencies)
        return f"Registry(assets={n_assets}, dependencies={n_deps})"

    def __len__(self) -> int:
        with self._lock:
            return len(self._assets)

    @property
    def assets(self) -> dict[str, Asset]:
        with self._lock:
            return self._assets

    @property
    def dependencies(self) -> list[Dependency]:
        with self._lock:
            return self._dependencies
