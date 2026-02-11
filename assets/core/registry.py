"""Registry — central store of assets and dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from assets.core.dependency import Dependency, FieldMapping
from assets.core.graph import AssetGraph, SelectionResult

if TYPE_CHECKING:
    from assets.core.asset import Asset
    from assets.resolver.ref import RefResolver


class Registry:
    """Central store of assets and dependencies with lazy graph construction."""

    def __init__(self, ref_resolver: RefResolver | None = None) -> None:
        from assets.resolver.ref import RefResolver

        self._assets: dict[str, Asset] = {}
        self._dependencies: list[Dependency] = []
        self._ref_resolver = ref_resolver or RefResolver()
        self._graph: AssetGraph | None = None

    def register(self, asset: Asset) -> None:
        """Register an asset. Extracts refs from SQL automatically."""
        self._assets[asset.name] = asset
        self._graph = None  # invalidate cached graph
        if asset.sql:
            refs = self._ref_resolver.extract_refs(asset.sql)
            asset.depends_on = refs
            for ref in refs:
                self._dependencies.append(
                    Dependency(source=ref, target=asset.name, type="ref")
                )

    def get(self, name: str) -> Asset | None:
        return self._assets.get(name)

    def all(self) -> list[Asset]:
        return list(self._assets.values())

    def clear(self) -> None:
        self._assets.clear()
        self._dependencies.clear()
        self._graph = None

    @property
    def graph(self) -> AssetGraph:
        """Lazily build DAG from current assets + dependencies."""
        if self._graph is None:
            self._graph = AssetGraph.build(self._assets, self._dependencies)
        return self._graph

    def select(self, selector: str) -> SelectionResult:
        """Query assets by selector expression."""
        return self.graph.select(selector)

    def resolve_column_lineage(
        self,
        asset_name: str | None = None,
        selector: str | None = None,
        resolver: object | None = None,
        force: bool = False,
    ) -> list[FieldMapping]:
        """Resolve column-level lineage using a consumer-provided resolver.

        The resolver must implement a `resolve(sql, schema)` method.
        This is intentionally generic — the library provides the base class,
        consumers bring their own implementation (e.g., sqlglot-based).
        """
        if resolver is None:
            raise ValueError("A lineage resolver instance must be provided by the consumer")

        targets: list[Asset] = []
        if asset_name:
            asset = self.get(asset_name)
            if asset:
                targets = [asset]
        elif selector:
            result = self.select(selector)
            targets = result.assets
        else:
            raise ValueError("Provide either asset_name or selector")

        mappings: list[FieldMapping] = []
        for asset in targets:
            if not asset.sql:
                continue
            resolved_sql = self._ref_resolver.resolve_sql(asset.sql)
            schema: dict[str, list[str]] = {}
            for dep_name in asset.depends_on:
                dep_asset = self.get(dep_name)
                if dep_asset:
                    schema[dep_name] = dep_asset.list_fields()
            result = resolver.resolve(resolved_sql, schema)  # type: ignore[attr-defined]
            mappings.extend(result)
        return mappings

    def __repr__(self) -> str:
        return f"Registry(assets={len(self._assets)}, dependencies={len(self._dependencies)})"

    def __len__(self) -> int:
        return len(self._assets)

    @property
    def assets(self) -> dict[str, Asset]:
        return self._assets

    @property
    def dependencies(self) -> list[Dependency]:
        return self._dependencies
