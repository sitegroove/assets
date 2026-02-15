"""Registry — central store of assets, dependencies, and resolvers.

Thread-safe: all mutations and the lazy graph property are guarded
by an ``RLock``.  ``RLock`` (not ``Lock``) is used because public
methods like ``register_many`` call internal helpers that also need
the lock, and ``resolve`` reads the graph while holding the lock.

Read-only methods (``get``, ``all``, ``__len__``, ``__contains__``)
acquire the same lock to guarantee a consistent snapshot.
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING, Any

from assets.core.dependency import Dependency, FieldMapping
from assets.core.graph import AssetGraph

if TYPE_CHECKING:
    from assets.core.asset import Asset
    from assets.resolver.lineage import DependencyResolver


class Registry:
    """Central store of assets, dependencies, and named resolvers.

    Consumers register assets via ``register()`` / ``register_many()``
    and optionally attach named resolvers for on-demand field-level
    dependency resolution::

        registry = Registry(resolvers={"lineage": ColumnLineageResolver()})
        registry.register_many(assets)

        # Resolve column lineage for a single asset
        mappings = registry.resolve("lineage", "mart_sales_pipeline")

        # Resolve for all assets matching a selector
        mappings = registry.resolve("lineage", selector="type:mart")

    Results are cached in-memory keyed on the target asset's fingerprint
    and its upstream assets' fingerprints.  The cache is automatically
    invalidated when assets are registered, unregistered, or cleared.

    All public methods are thread-safe.
    """

    def __init__(
        self,
        resolvers: dict[str, DependencyResolver] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._assets: dict[str, Asset] = {}
        self._dependencies: list[Dependency] = []
        self._graph: AssetGraph | None = None
        self._resolvers: dict[str, DependencyResolver] = dict(resolvers or {})
        # Cache: (kind, asset_id) -> (cache_key, list[FieldMapping])
        self._resolve_cache: dict[tuple[str, str], tuple[str, list[FieldMapping]]] = {}

    # ── Resolver management ──────────────────────────────────────

    def add_resolver(self, name: str, resolver: DependencyResolver) -> None:
        """Register a named resolver.

        Args:
            name: Resolver name (e.g. ``"lineage"``, ``"metrics"``).
            resolver: A :class:`DependencyResolver` subclass instance.

        Raises:
            ValueError: If a resolver with the same name already exists.
        """
        with self._lock:
            if name in self._resolvers:
                raise ValueError(
                    f"Resolver '{name}' already registered. "
                    f"Available: {sorted(self._resolvers.keys())}"
                )
            self._resolvers[name] = resolver

    def resolve(
        self,
        kind: str,
        asset_id: str | None = None,
        *,
        selector: str | None = None,
    ) -> list[FieldMapping]:
        """Resolve field-level dependencies using a named resolver.

        Results are cached per ``(kind, asset_id)`` keyed on the asset's
        fingerprint and its upstream assets' fingerprints.  The cache is
        invalidated automatically when assets change.

        Args:
            kind: Name of the registered resolver (e.g. ``"lineage"``).
            asset_id: Resolve for a single asset by ID.
            selector: Resolve for all assets matching a selector
                expression.  Exactly one of ``asset_id`` or ``selector``
                must be provided.

        Returns:
            List of :class:`FieldMapping` entries.

        Raises:
            ValueError: If the resolver name is unknown or neither
                ``asset_id`` nor ``selector`` is provided.
        """
        with self._lock:
            resolver = self._resolvers.get(kind)
            if resolver is None:
                raise ValueError(
                    f"Unknown resolver '{kind}'. "
                    f"Available: {sorted(self._resolvers.keys())}"
                )
        assert resolver is not None

        targets = self._resolve_targets(asset_id, selector)

        mappings: list[FieldMapping] = []
        for asset in targets:
            cache_key_tuple = (kind, asset.id)
            current_key = self._build_cache_key(asset)

            with self._lock:
                cached = self._resolve_cache.get(cache_key_tuple)
            if cached is not None and cached[0] == current_key:
                mappings.extend(cached[1])
                continue

            schema = self._build_schema(asset)
            result = resolver.resolve(asset, schema)
            with self._lock:
                self._resolve_cache[cache_key_tuple] = (current_key, result)
            mappings.extend(result)

        return mappings

    # ── Backward-compatible resolve_field_dependency ──────────────

    def resolve_field_dependency(
        self,
        asset_id: str | None = None,
        selector: str | None = None,
        resolver: DependencyResolver | None = None,
    ) -> list[FieldMapping]:
        """Resolve field-level dependencies using a consumer-provided resolver.

        .. deprecated::
            Use :meth:`resolve` with named resolvers instead.  This method
            is kept for backward compatibility.

        The resolver must implement ``resolve(asset, schema)``.
        """
        if resolver is None:
            raise ValueError("A resolver instance must be provided by the consumer")

        targets = self._resolve_targets(asset_id, selector)

        mappings: list[FieldMapping] = []
        for asset in targets:
            schema = self._build_schema(asset)
            result = resolver.resolve(asset, schema)
            mappings.extend(result)
        return mappings

    # ── Asset management ─────────────────────────────────────────

    def register(self, asset: Asset) -> None:
        """Register an asset and build dependency edges from ``depends_on``."""
        with self._lock:
            self._assets[asset.id] = asset
            self._invalidate()
            # Remove old deps for this asset to avoid duplication
            self._dependencies = [d for d in self._dependencies if d.target != asset.id]
            for dep_name in dict.fromkeys(asset.depends_on):
                self._dependencies.append(
                    Dependency(source=dep_name, target=asset.id, type="ref")
                )

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
            self._invalidate()

    def unregister(self, name: str) -> bool:
        """Remove an asset and its dependency edges. Returns True if found."""
        with self._lock:
            if name not in self._assets:
                return False
            del self._assets[name]
            self._dependencies = [
                d for d in self._dependencies if d.source != name and d.target != name
            ]
            self._invalidate()
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
            self._invalidate()
            return removed

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
            self._invalidate()

    # ── Graph ────────────────────────────────────────────────────

    @property
    def graph(self) -> AssetGraph:
        """Lazily build DAG from current assets + dependencies."""
        with self._lock:
            if self._graph is None:
                self._graph = AssetGraph.build(self._assets, self._dependencies)
            return self._graph

    # ── Export ────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Export the full catalog as a serializable dict."""
        return self.graph.to_dict()

    def to_mermaid(self) -> str:
        """Export the asset graph as a Mermaid flowchart."""
        return self.graph.to_mermaid()

    # ── Properties ───────────────────────────────────────────────

    @property
    def assets(self) -> dict[str, Asset]:
        with self._lock:
            return self._assets

    @property
    def dependencies(self) -> list[Dependency]:
        with self._lock:
            return self._dependencies

    def __repr__(self) -> str:
        with self._lock:
            n_assets = len(self._assets)
            n_deps = len(self._dependencies)
            n_resolvers = len(self._resolvers)
        if n_resolvers:
            return (
                f"Registry(assets={n_assets}, dependencies={n_deps}, "
                f"resolvers={n_resolvers})"
            )
        return f"Registry(assets={n_assets}, dependencies={n_deps})"

    def __len__(self) -> int:
        with self._lock:
            return len(self._assets)

    # ── Internal helpers ─────────────────────────────────────────

    def _invalidate(self) -> None:
        """Invalidate graph and resolve caches."""
        self._graph = None
        self._resolve_cache.clear()

    def _resolve_targets(
        self,
        asset_id: str | None,
        selector: str | None,
    ) -> list[Asset]:
        """Collect target assets from asset_id or selector."""
        targets: list[Asset] = []
        if asset_id:
            asset = self.get(asset_id)
            if asset:
                targets = [asset]
        elif selector:
            from assets.selector.parser import GraphSelector

            result = GraphSelector(self).execute(selector)
            targets = result.assets
        else:
            raise ValueError("Provide either asset_id or selector")
        return targets

    def _build_schema(self, asset: Asset) -> dict[str, list[str]]:
        """Build upstream schema dict for a target asset.

        Values are namespaced paths (``"field_name/child_id"``) so that
        resolvers can unambiguously identify child assets even when
        multiple child fields exist on the upstream asset.
        """
        schema: dict[str, list[str]] = {}
        for dep_name in asset.depends_on:
            dep_asset = self.get(dep_name)
            if dep_asset:
                schema[dep_name] = [
                    f"{field_name}/{child.id}"
                    for field_name, items in dep_asset._child_fields().items()
                    for child in items
                ]
        return schema

    def _build_cache_key(self, asset: Asset) -> str:
        """Build a cache key from asset + upstream fingerprints."""
        parts = [asset.fingerprint]
        for dep_name in sorted(asset.depends_on):
            dep_asset = self.get(dep_name)
            if dep_asset:
                parts.append(dep_asset.fingerprint)
        combined = ":".join(parts)
        return hashlib.sha256(combined.encode()).hexdigest()
