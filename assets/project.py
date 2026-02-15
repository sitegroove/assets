"""High-level facade API for common assets workflows."""

from __future__ import annotations

from pathlib import Path

from assets.core.asset import Asset
from assets.core.dependency import FieldMapping
from assets.core.graph import AssetGraph, SelectionResult
from assets.core.registry import Registry
from assets.engine.manager import ApplyResult, StateManager
from assets.engine.planner import Plan
from assets.loader.discovery import FileDiscovery, LoadResult, SourceGroup
from assets.resolver.lineage import DependencyResolver
from assets.selector.parser import GraphSelector
from assets.state.backend import StateBackend
from assets.state.environment import Environment, EnvironmentConfig
from assets.state.sqlite import SQLiteBackend


class Project:
    """High-level facade for registry, selectors, and state operations."""

    def __init__(
        self,
        environment: str = "default",
        *,
        state_dir: str | None = None,
        backend: StateBackend | None = None,
        env_config: EnvironmentConfig | None = None,
        resolvers: dict[str, DependencyResolver] | None = None,
        protected_environments: set[str] | None = None,
    ) -> None:
        if state_dir is not None and backend is not None:
            raise ValueError("Provide only one of state_dir or backend")

        self.environment = environment
        self._registry = Registry(resolvers=resolvers)
        self._manager: StateManager | None = None

        if state_dir is None and backend is None:
            return

        if backend is None:
            backend = SQLiteBackend(db_path=Path(state_dir) / "state.db")

        if env_config is None:
            env_config = EnvironmentConfig(
                default=environment,
                environments={environment: Environment(name=environment)},
                protected=protected_environments or {"production"},
            )
        elif protected_environments is not None:
            env_config.protected = set(protected_environments)

        self._manager = StateManager(
            self._registry,
            backend,
            env_config,
            environment=environment,
        )

    @property
    def registry(self) -> Registry:
        """Underlying registry escape hatch."""
        return self._registry

    @property
    def manager(self) -> StateManager | None:
        """Underlying state manager escape hatch (if configured)."""
        return self._manager

    @property
    def graph(self) -> AssetGraph:
        """Return the lazily-built asset graph."""
        return self._registry.graph

    def register(self, asset: Asset) -> None:
        """Register a single asset."""
        self._registry.register(asset)

    def register_many(self, assets: list[Asset]) -> None:
        """Register multiple assets in one batch."""
        self._registry.register_many(assets)

    def unregister(self, asset_id: str) -> bool:
        """Unregister an asset by ID."""
        return self._registry.unregister(asset_id)

    def get(self, asset_id: str) -> Asset | None:
        """Get a single asset by ID."""
        return self._registry.get(asset_id)

    def all(self) -> list[Asset]:
        """List all registered assets."""
        return self._registry.all()

    def clear(self) -> None:
        """Clear all registered assets and dependencies."""
        self._registry.clear()

    def select(self, selector: str) -> SelectionResult:
        """Execute a selector expression (graph, state, or combined)."""
        return GraphSelector(self._registry, manager=self._manager).execute(
            selector, environment=self.environment
        )

    def add_resolver(self, name: str, resolver: DependencyResolver) -> None:
        """Register a named dependency resolver."""
        self._registry.add_resolver(name, resolver)

    def resolve(
        self,
        kind: str,
        asset_id: str | None = None,
        *,
        selector: str | None = None,
    ) -> list[FieldMapping]:
        """Resolve field-level dependencies through a named resolver."""
        return self._registry.resolve(kind, asset_id, selector=selector)

    def plan(self, selector: str | None = None) -> Plan:
        """Plan changes for the active environment."""
        manager = self._require_manager()
        return manager.plan(selector=selector)

    def apply(self, plan: Plan) -> ApplyResult:
        """Apply a previously created plan."""
        manager = self._require_manager()
        return manager.apply(plan)

    def drift(self) -> Plan:
        """Detect drift for the active environment."""
        manager = self._require_manager()
        return manager.drift()

    def promote_to(self, target_environment: str, selector: str | None = None) -> Plan:
        """Plan promotion from active environment to target environment."""
        manager = self._require_manager()
        return manager.promote_to(target_environment, selector=selector)

    def create_environment(
        self,
        name: str,
        parent: str | None = None,
        shallow: bool = True,
    ) -> Environment:
        """Create a new environment."""
        manager = self._require_manager()
        return manager.create_environment(name, parent=parent, shallow=shallow)

    def destroy_environment(self, name: str) -> None:
        """Destroy an environment, unless protected."""
        manager = self._require_manager()
        manager.destroy_environment(name)

    def load(self, groups: list[SourceGroup]) -> LoadResult:
        """Run file discovery/loading for source groups."""
        manager = self._require_manager()
        return FileDiscovery(groups).load(manager, environment=self.environment)

    def to_dict(self) -> dict[str, object]:
        """Export the registry as a dictionary."""
        return self._registry.to_dict()

    def to_mermaid(self) -> str:
        """Export the graph as a Mermaid flowchart string."""
        return self._registry.to_mermaid()

    def _require_manager(self) -> StateManager:
        if self._manager is None:
            raise RuntimeError(
                "No state backend configured. Pass state_dir= or backend= to Project()."
            )
        return self._manager

    def __len__(self) -> int:
        return len(self._registry)

    def __contains__(self, asset_id: str) -> bool:
        return self._registry.get(asset_id) is not None

    def __repr__(self) -> str:
        return (
            f"Project(environment={self.environment!r}, assets={len(self)}, "
            f"state_enabled={self._manager is not None})"
        )
