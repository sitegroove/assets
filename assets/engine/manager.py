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
    """Resolved state for an environment (no parent chain)."""

    assets: dict[str, AssetState] = Field(default_factory=dict)
    dependencies: list[DependencyState] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StateManager:
    """Main orchestrator: combines registry, backend, environments.

    Each environment is physically isolated in its own database.
    Creating an environment copies the parent's state (copy-on-create),
    after which the two are fully independent.

    Typical usage::

        manager = StateManager.create(registry, local_path=".assets_state")
        plan = manager.plan()
        manager.apply(plan)
    """

    def __init__(
        self,
        registry: Registry,
        backend: StateBackend,
        env_config: EnvironmentConfig,
        environment: str | None = None,
    ) -> None:
        self.registry = registry
        self.backend = backend
        self.env_config = env_config
        self.environment = self.env_config.get(environment).name
        self._differ = Differ()
        self._index: FileIndex | None = None

    @classmethod
    def create(
        cls,
        registry: Registry,
        *,
        local_path: str,
        environments: dict[str, Environment] | None = None,
        default_env: str = "default",
        environment: str | None = None,
        protected_environments: set[str] | None = None,
    ) -> StateManager:
        """Convenience factory for common setups.

        Args:
            registry: Registry containing the assets to manage.
            local_path: Base directory for per-environment state
                databases and the local file index.
            environments: Dict of environments. Defaults to a single
                default env.
            default_env: Default environment name.
            environment: Active environment for manager operations.
                Falls back to ``default_env`` when omitted.
            protected_environments: Environment names protected from deletion.
        """
        from assets.state.sqlite import SQLiteBackend

        backend = SQLiteBackend(base_path=local_path)

        if environments is None:
            environments = {default_env: Environment(name=default_env)}

        env_config = EnvironmentConfig(
            default=default_env,
            environments=environments,
            protected=protected_environments or {"production"},
        )
        return cls(
            registry,
            backend,
            env_config,
            environment=environment or default_env,
        )

    @property
    def index(self) -> FileIndex:
        """Lazy :class:`~assets.index.file.FileIndex` for file change detection.

        The file index lives in a separate ``index.db`` at the base
        state directory (never synced to remote).  Requires a
        file-backed :class:`~assets.state.sqlite.SQLiteBackend`
        (or :class:`~assets.state.tiered.TieredBackend`) as backend.
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
                "for the file index."
            )

        index_db_path = local_path / "index.db"
        self._index = FileIndex(index_db_path, local_path)
        return self._index

    def plan(
        self,
        selector: str | None = None,
        *,
        environment: str | None = None,
    ) -> Plan:
        """Detect changes between current registry and applied state."""
        env = self.env_config.get(environment or self.environment)

        # Get desired assets (optionally filtered by selector)
        if selector:
            selection = GraphSelector(self.registry).execute(selector)
            desired = selection.assets
        else:
            desired = self.registry.all()

        # Load current state (each env is its own DB, no parent chain)
        resolved = self._resolve_state(env)

        # Diff
        changeset = self._differ.diff(desired, resolved.assets)

        return Plan(
            changeset=changeset,
            environment=env.name,
        )

    def apply(self, plan: Plan, *, environment: str | None = None) -> ApplyResult:
        """Apply a plan to state. Acquires lock, writes changes, releases."""
        env_name = environment or plan.environment
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

    def drift(self, *, environment: str | None = None) -> Plan:
        """Detect drift: compare state against current registry.

        Same as plan() — compares what's in registry vs. what's in state.
        """
        return self.plan(environment=environment)

    def promote_to(
        self,
        to_env: str,
        selector: str | None = None,
        *,
        from_env: str | None = None,
    ) -> Plan:
        """Generate plan to promote changes from one env to another."""
        source_env = from_env or self.environment
        source_state = self.backend.load(source_env)
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
                if asset_state:
                    desired_assets.append(Asset.model_validate(asset_state.data))

        changeset = self._differ.diff(desired_assets, target_resolved.assets)

        return Plan(
            changeset=changeset,
            environment=to_env,
        )

    def promote(
        self,
        from_env: str,
        to_env: str,
        selector: str | None = None,
    ) -> Plan:
        """Backward-compatible promote alias."""
        return self.promote_to(to_env, selector=selector, from_env=from_env)

    def create_environment(
        self,
        name: str,
        parent: str | None = None,
    ) -> Environment:
        """Create a new environment by copying the parent's state.

        Uses copy-on-create semantics: the parent's ``state.db`` is
        copied into a new directory for the child environment.  From
        that point on, the two environments are fully independent.

        Args:
            name: Name for the new environment.
            parent: Source environment to copy from.  Defaults to the
                active environment.

        Returns:
            The newly created :class:`Environment`.
        """
        parent_name = parent or self.environment
        self.backend.copy_environment(parent_name, name)
        env = Environment(name=name)
        self.env_config.environments[name] = env
        return env

    def destroy_environment(self, name: str) -> None:
        """Delete env and state. Protected envs cannot be destroyed."""
        if name in self.env_config.protected:
            raise ValueError(f"Cannot destroy protected environment '{name}'")
        self.backend.delete_environment(name)
        self.env_config.environments.pop(name, None)

    def _resolve_state(self, env: Environment) -> ResolvedState:
        """Load state for an environment.

        Each environment is its own database — no parent chain walking.
        """
        state = self.backend.load(env.name)
        if state is None:
            return ResolvedState()
        return ResolvedState(
            assets=dict(state.assets),
            dependencies=list(state.dependencies),
            metadata=dict(state.metadata),
        )
