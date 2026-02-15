"""StateManager — orchestrates plan/apply/promote/drift workflows."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from assets.core.registry import Registry
from assets.engine.differ import Differ
from assets.engine.planner import Plan
from assets.selector.parser import GraphSelector
from assets.state.backend import StateBackend
from assets.state.environment import Environment, EnvironmentConfig
from assets.state.models import AssetState, DependencyState, StateSnapshot

if TYPE_CHECKING:
    from assets.index.file import FileIndex

PROTECTED_ENVIRONMENTS = {"production", "staging"}


class ApplyResult(BaseModel):
    """Result of applying a plan."""

    applied: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    environment: str = ""

    def __repr__(self) -> str:
        return (
            f"ApplyResult(environment={self.environment!r}, "
            f"created={self.created}, updated={self.updated}, deleted={self.deleted})"
        )


class ResolvedState(BaseModel):
    """State after walking parent chain and merging layers."""

    assets: dict[str, AssetState] = Field(default_factory=dict)
    dependencies: list[DependencyState] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StateManager:
    """Main orchestrator: combines registry, backend, environments.

    Typical usage::

        manager = StateManager.create(registry, local_path=".assets_state")
        plan = manager.plan(environment="production")
        manager.apply(plan)
    """

    def __init__(
        self,
        registry: Registry,
        backend: StateBackend,
        env_config: EnvironmentConfig,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.env_config = env_config
        self._differ = Differ()
        self._index: FileIndex | None = None

    @classmethod
    def create(
        cls,
        registry: Registry,
        *,
        local_path: str,
        environments: dict[str, Environment] | None = None,
        default_env: str = "production",
    ) -> StateManager:
        """Convenience factory for common setups.

        Args:
            registry: Registry containing the assets to manage.
            local_path: Directory for the SQLite state database.
            environments: Dict of environments. Defaults to a single
                production env.
            default_env: Default environment name.
        """
        from assets.state.sqlite import SQLiteBackend

        backend = SQLiteBackend(db_path=Path(local_path) / "state.db")

        if environments is None:
            environments = {default_env: Environment(name=default_env)}

        env_config = EnvironmentConfig(
            default=default_env,
            environments=environments,
        )
        return cls(registry, backend, env_config)

    @property
    def index(self) -> FileIndex:
        """Lazy :class:`~assets.index.file.FileIndex` for file change detection.

        Requires a file-backed :class:`~assets.state.sqlite.SQLiteBackend`
        (or :class:`~assets.state.tiered.TieredBackend`) as the backend.
        In-memory backends (``SQLiteBackend(":memory:")``) do not support
        file indexing because there is no filesystem directory for the
        mtime cache.
        """
        if self._index is not None:
            return self._index

        from assets.index.file import FileIndex
        from assets.state.sqlite import SQLiteBackend
        from assets.state.tiered import TieredBackend

        if isinstance(self.backend, TieredBackend):
            backend = self.backend._local
        elif isinstance(self.backend, SQLiteBackend):
            backend = self.backend
        else:
            raise RuntimeError(
                "FileIndex requires a SQLiteBackend or "
                "TieredBackend. Got: "
                f"{type(self.backend).__name__}"
            )

        local_path = backend.local_path
        if local_path is None:
            raise RuntimeError(
                "FileIndex requires a file-backed SQLiteBackend. "
                "In-memory backends do not have a local path "
                "for the mtime cache."
            )

        self._index = FileIndex(backend.conn, local_path)
        return self._index

    def plan(
        self,
        environment: str | None = None,
        selector: str | None = None,
    ) -> Plan:
        """Detect changes between current registry and applied state."""
        env = self.env_config.get(environment)

        # Get desired assets (optionally filtered by selector)
        if selector:
            selection = GraphSelector(self.registry).execute(selector)
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
        if not plan.changeset.asset_changes:
            return ApplyResult(environment=env_name)

        with self.backend.lock(env_name):
            state = self.backend.load(env_name)
            if state is None:
                state = StateSnapshot(environment=env_name)

            now = datetime.now(timezone.utc)
            created = 0
            updated = 0
            deleted = 0

            for change in plan.changeset.asset_changes:
                if change.action in ("create", "update"):
                    existing = state.assets.get(change.asset_id)
                    new_version = (existing.version + 1) if existing else 1
                    state.assets[change.asset_id] = AssetState(
                        id=change.asset_id,
                        type=(change.after.get("type", "") if change.after else ""),
                        fingerprint=change.after_fingerprint or "",
                        data=change.after or {},
                        applied_at=now,
                        version=new_version,
                    )
                    if change.action == "create":
                        created += 1
                    else:
                        updated += 1

                elif change.action == "delete":
                    if env.shallow:
                        # Tombstone for shallow envs
                        existing = state.assets.get(change.asset_id)
                        if existing:
                            existing.deleted = True
                            existing.applied_at = now
                    else:
                        state.assets.pop(change.asset_id, None)
                    deleted += 1

            state.updated_at = now
            changed_ids = plan.changed_ids
            self.backend.save(env_name, state, changed_ids=changed_ids)

        return ApplyResult(
            applied=created + updated + deleted,
            created=created,
            updated=updated,
            deleted=deleted,
            environment=env_name,
        )

    def drift(self, environment: str | None = None) -> Plan:
        """Detect drift: compare state against current registry.

        Same as plan() — compares what's in registry vs. what's in state.
        """
        return self.plan(environment=environment)

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
            source_registry = Registry()
            source_registry.register_many(
                [
                    Asset.model_validate(asset_state.data)
                    for asset_state in source_assets.values()
                    if not asset_state.deleted
                ]
            )
            selected_names = GraphSelector(source_registry).execute(selector).names
            desired_assets = [
                asset
                for name in selected_names
                if (asset := source_registry.get(name)) is not None
            ]
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
        if parent not in self.env_config.environments:
            raise ValueError(
                f"Parent environment '{parent}' does not exist. "
                f"Available: {sorted(self.env_config.environments.keys())}"
            )
        env = Environment(name=name, parent=parent, shallow=shallow)
        self.env_config.environments[name] = env
        return env

    def destroy_environment(self, name: str) -> None:
        """Delete env and state. Protected envs cannot be destroyed."""
        if name in PROTECTED_ENVIRONMENTS:
            raise ValueError(f"Cannot destroy protected environment '{name}'")
        self.backend.delete_environment(name)
        self.env_config.environments.pop(name, None)

    def _resolve_state(
        self, env: Environment, _seen: set[str] | None = None
    ) -> ResolvedState:
        """Walk parent chain, merge state layers. Local overrides parent."""
        if _seen is None:
            _seen = set()
        if env.name in _seen:
            raise ValueError(
                f"Circular parent reference detected: '{env.name}' "
                f"already visited in chain {sorted(_seen)}"
            )
        _seen.add(env.name)

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
        parent_resolved = self._resolve_state(parent_env, _seen)

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
            dependencies=(
                state.dependencies
                if state.dependencies is not None
                else parent_resolved.dependencies
            ),
            metadata={**parent_resolved.metadata, **state.metadata},
        )
