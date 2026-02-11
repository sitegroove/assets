"""StateManager — orchestrates plan/apply/promote/drift workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from assets.core.registry import Registry
from assets.engine.differ import Differ
from assets.engine.planner import Plan
from assets.loader.project import ProjectLoader
from assets.state.backend import StateBackend
from assets.state.environment import Environment, EnvironmentConfig
from assets.state.models import AssetState, DependencyState, StateSnapshot

PROTECTED_ENVIRONMENTS = {"production", "staging"}


class ApplyResult(BaseModel):
    """Result of applying a plan."""

    applied: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    environment: str = ""


class ResolvedState(BaseModel):
    """State after walking parent chain and merging layers."""

    assets: dict[str, AssetState] = {}
    dependencies: list[DependencyState] = []
    metadata: dict[str, Any] = {}


class StateManager:
    """Main orchestrator: combines loader, registry, backend, environments."""

    def __init__(
        self,
        registry: Registry,
        loader: ProjectLoader,
        backend: StateBackend,
        env_config: EnvironmentConfig,
    ) -> None:
        self.registry = registry
        self.loader = loader
        self.backend = backend
        self.env_config = env_config
        self._differ = Differ()

    def plan(
        self,
        project_dir: str,
        environment: str | None = None,
        selector: str | None = None,
    ) -> Plan:
        """Detect changes between files on disk and applied state."""
        env = self.env_config.get(environment)

        # Load all assets from project
        self.registry.clear()
        self.loader.load(project_dir)

        # Get desired assets (optionally filtered by selector)
        if selector:
            selection = self.registry.select(selector)
            desired = selection.assets
        else:
            desired = self.registry.all()

        # Resolve current state (walk parent chain for shallow envs)
        resolved = self._resolve_state(env)

        # Diff
        changeset = self._differ.diff(desired, resolved.assets)

        return Plan(
            changeset=changeset,
            environment=env.name,
        )

    def apply(self, plan: Plan, environment: str | None = None) -> ApplyResult:
        """Apply a plan to state. Acquires lock, writes changes, releases."""
        env_name = environment or plan.environment
        env = self.env_config.get(env_name)

        with self.backend.lock(env_name):
            state = self.backend.load(env_name)
            if state is None:
                state = StateSnapshot(environment=env_name)

            now = datetime.now(UTC)
            created = 0
            updated = 0
            deleted = 0

            for change in plan.changeset.asset_changes:
                if change.action == "create":
                    state.assets[change.asset_name] = AssetState(
                        name=change.asset_name,
                        kind=change.after.get("kind", "") if change.after else "",
                        fingerprint=change.after_fingerprint or "",
                        data=change.after or {},
                        applied_at=now,
                    )
                    created += 1

                elif change.action == "update":
                    state.assets[change.asset_name] = AssetState(
                        name=change.asset_name,
                        kind=change.after.get("kind", "") if change.after else "",
                        fingerprint=change.after_fingerprint or "",
                        data=change.after or {},
                        applied_at=now,
                    )
                    updated += 1

                elif change.action == "delete":
                    if env.shallow:
                        # Tombstone for shallow envs
                        existing = state.assets.get(change.asset_name)
                        if existing:
                            existing.deleted = True
                            existing.applied_at = now
                    else:
                        state.assets.pop(change.asset_name, None)
                    deleted += 1

            state.updated_at = now
            self.backend.save(env_name, state)

        return ApplyResult(
            applied=created + updated + deleted,
            created=created,
            updated=updated,
            deleted=deleted,
            environment=env_name,
        )

    def drift(self, project_dir: str, environment: str | None = None) -> Plan:
        """Detect drift: compare state against current files.

        Same as plan() — compares what's on disk vs. what's in state.
        """
        return self.plan(project_dir, environment=environment)

    def promote(
        self,
        from_env: str,
        to_env: str,
        selector: str | None = None,
    ) -> Plan:
        """Generate plan to promote changes from one env to another."""
        source_state = self.backend.load(from_env)
        if source_state is None:
            return Plan(environment=to_env)

        target_env = self.env_config.get(to_env)
        target_resolved = self._resolve_state(target_env)

        # Build "desired" from source state
        from assets.core.asset import Asset

        desired_assets: list[Asset] = []
        source_assets = source_state.assets

        if selector:
            # Filter source assets by name matching
            # Load registry to use selector
            from assets.core.graph import AssetGraph

            temp_assets = {}
            for name, asset_state in source_assets.items():
                if not asset_state.deleted:
                    temp_assets[name] = Asset.model_validate(asset_state.data)

            graph = AssetGraph.build(temp_assets, [])
            selection = graph.select(selector)
            selected_names = selection.names
        else:
            selected_names = set(source_assets.keys())

        for name in selected_names:
            asset_state = source_assets.get(name)
            if asset_state and not asset_state.deleted:
                desired_assets.append(Asset.model_validate(asset_state.data))

        changeset = self._differ.diff(desired_assets, target_resolved.assets)

        return Plan(
            changeset=changeset,
            environment=to_env,
        )

    def create_environment(
        self,
        name: str,
        parent: str = "production",
        shallow: bool = True,
    ) -> Environment:
        """Create a new environment."""
        env = Environment(name=name, parent=parent, shallow=shallow)
        self.env_config.environments[name] = env
        return env

    def destroy_environment(self, name: str) -> None:
        """Delete env and state. Protected envs cannot be destroyed."""
        if name in PROTECTED_ENVIRONMENTS:
            raise ValueError(f"Cannot destroy protected environment '{name}'")
        self.backend.delete_environment(name)
        self.env_config.environments.pop(name, None)

    def _resolve_state(self, env: Environment) -> ResolvedState:
        """Walk parent chain, merge state layers. Local overrides parent."""
        state = self.backend.load(env.name)

        if not env.shallow or env.parent is None:
            # Full environment — just return its state
            if state is None:
                return ResolvedState()
            return ResolvedState(
                assets=dict(state.assets),
                dependencies=list(state.dependencies),
                metadata=dict(state.metadata),
            )

        # Shallow environment — resolve parent first, then overlay
        parent_env = self.env_config.get(env.parent)
        parent_resolved = self._resolve_state(parent_env)

        if state is None:
            return parent_resolved

        # Overlay local state on parent
        merged_assets = dict(parent_resolved.assets)
        for name, asset_state in state.assets.items():
            if asset_state.deleted:
                merged_assets.pop(name, None)
            else:
                merged_assets[name] = asset_state

        return ResolvedState(
            assets=merged_assets,
            dependencies=state.dependencies or parent_resolved.dependencies,
            metadata={**parent_resolved.metadata, **state.metadata},
        )
