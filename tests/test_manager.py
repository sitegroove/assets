"""Tests for StateManager."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assets import (
    Asset,
    Environment,
    EnvironmentConfig,
    Registry,
    SQLiteBackend,
    StateManager,
)
from assets.state.models import AssetState, DependencyState, StateSnapshot


def _load_json_assets(
    registry: Registry,
    models_dir: Path,
    asset_class: type[Asset] = Asset,
) -> None:
    """Load JSON asset files into the registry."""
    for path in sorted(models_dir.rglob("*.json")):
        data = json.loads(path.read_text())
        registry.register(asset_class.model_validate(data))


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    models = tmp_path / "models"
    models.mkdir()
    (models / "users.json").write_text(
        json.dumps({"id": "raw.users", "type": "source", "tags": ["raw"]})
    )
    (models / "orders.json").write_text(
        json.dumps({"id": "raw.orders", "type": "source", "tags": ["raw"]})
    )
    return tmp_path


@pytest.fixture
def env_config() -> EnvironmentConfig:
    return EnvironmentConfig(
        default="development",
        environments={
            "production": Environment(name="production"),
            "staging": Environment(name="staging", parent="production"),
            "development": Environment(
                name="development", parent="production", shallow=True
            ),
        },
    )


@pytest.fixture
def manager(project_dir: Path, env_config: EnvironmentConfig) -> StateManager:
    registry = Registry()
    backend = SQLiteBackend.memory()
    return StateManager(registry, backend, env_config)


class TestStateManager:
    def test_plan_first_run(self, manager: StateManager, project_dir: Path):
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        assert plan.has_changes
        creates = [c for c in plan.changeset.asset_changes if c.action == "create"]
        assert len(creates) == 2

    def test_plan_no_changes_after_apply(
        self, manager: StateManager, project_dir: Path
    ):
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan2 = manager.plan(environment="production")
        assert not plan2.has_changes

    def test_apply_result(self, manager: StateManager, project_dir: Path):
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        result = manager.apply(plan, environment="production")
        assert result.created == 2
        assert result.applied == 2

    def test_plan_detects_update(self, manager: StateManager, project_dir: Path):
        # First apply
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Modify a file
        users_path = project_dir / "models" / "users.json"
        users_path.write_text(
            json.dumps({"id": "raw.users", "type": "source_v2", "tags": ["raw"]})
        )

        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan2 = manager.plan(environment="production")
        assert plan2.has_changes
        updates = [c for c in plan2.changeset.asset_changes if c.action == "update"]
        assert len(updates) == 1
        assert updates[0].asset_id == "raw.users"

    def test_plan_detects_delete(self, manager: StateManager, project_dir: Path):
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Delete a file
        (project_dir / "models" / "orders.json").unlink()

        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan2 = manager.plan(environment="production")
        assert plan2.has_changes
        deletes = [c for c in plan2.changeset.asset_changes if c.action == "delete"]
        assert len(deletes) == 1
        assert deletes[0].asset_id == "raw.orders"

    def test_plan_with_selector(self, manager: StateManager, project_dir: Path):
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(
            environment="production",
            selector="raw.users",
        )
        creates = [c for c in plan.changeset.asset_changes if c.action == "create"]
        assert len(creates) == 1
        assert creates[0].asset_id == "raw.users"

    def test_shallow_env_inherits_parent(
        self, manager: StateManager, project_dir: Path
    ):
        # Apply to production
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Plan for shallow dev — should see no changes since parent has everything
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan_dev = manager.plan(environment="development")
        assert not plan_dev.has_changes

    def test_promote(self, manager: StateManager, project_dir: Path):
        # Apply to production
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Promote production → staging
        promote_plan = manager.promote(from_env="production", to_env="staging")
        assert promote_plan.has_changes
        manager.apply(promote_plan, environment="staging")

        # Promote again — no changes
        promote_plan2 = manager.promote(from_env="production", to_env="staging")
        assert not promote_plan2.has_changes

    def test_create_environment(self, manager: StateManager):
        env = manager.create_environment("pr-123", parent="production", shallow=True)
        assert env.name == "pr-123"
        assert env.shallow is True
        assert "pr-123" in manager.env_config.environments

    def test_destroy_environment(self, manager: StateManager):
        manager.create_environment("temp")
        manager.destroy_environment("temp")
        assert "temp" not in manager.env_config.environments

    def test_destroy_protected_raises(self, manager: StateManager):
        with pytest.raises(ValueError, match="protected"):
            manager.destroy_environment("production")

    def test_drift(self, manager: StateManager, project_dir: Path):
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # No drift initially
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        drift = manager.drift(environment="production")
        assert not drift.has_changes

    def test_apply_empty_changeset_is_noop(
        self, manager: StateManager, project_dir: Path
    ):
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Second plan has no changes
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan2 = manager.plan(environment="production")
        assert not plan2.has_changes

        # Apply should be a noop (no I/O, no lock)
        result = manager.apply(plan2, environment="production")
        assert result.applied == 0
        assert result.created == 0

    def test_create_environment_invalid_parent_raises(self, manager: StateManager):
        with pytest.raises(ValueError, match="does not exist"):
            manager.create_environment("pr-123", parent="nonexistent")

    def test_promote_missing_source_env(self, manager: StateManager):
        # Source env has no state yet — should return empty plan
        plan = manager.promote(from_env="production", to_env="staging")
        # production has no state, so promote produces empty plan
        assert not plan.has_changes

    def test_promote_selector_graph_traversal_uses_dependencies(
        self, manager: StateManager
    ):
        source = StateSnapshot(
            environment="production",
            assets={
                "raw.users": AssetState(
                    id="raw.users",
                    type="source",
                    fingerprint="fp-raw",
                    data={"id": "raw.users", "type": "source"},
                ),
                "staging.users": AssetState(
                    id="staging.users",
                    type="data_model",
                    fingerprint="fp-staging",
                    data={
                        "id": "staging.users",
                        "type": "data_model",
                        "depends_on": ["raw.users"],
                    },
                ),
                "mart.enriched": AssetState(
                    id="mart.enriched",
                    type="data_model",
                    fingerprint="fp-mart",
                    data={
                        "id": "mart.enriched",
                        "type": "data_model",
                        "depends_on": ["staging.users"],
                    },
                ),
            },
        )
        manager.backend.save("production", source)

        plan = manager.promote(
            from_env="production",
            to_env="staging",
            selector="+mart.enriched",
        )
        selected = {c.asset_id for c in plan.changeset.asset_changes}
        assert selected == {"raw.users", "staging.users", "mart.enriched"}

    def test_circular_parent_env_raises(self):
        config = EnvironmentConfig(
            default="a",
            environments={
                "a": Environment(name="a", parent="b", shallow=True),
                "b": Environment(name="b", parent="a", shallow=True),
            },
        )
        mgr = StateManager(
            Registry(),
            SQLiteBackend.memory(),
            config,
        )
        with pytest.raises(ValueError, match="Circular parent reference"):
            mgr._resolve_state(config.environments["a"])


class TestApplyVersionIncrement:
    """Verify that apply() increments AssetState.version on updates."""

    @pytest.fixture
    def project_dir(self, tmp_path: Path) -> Path:
        models = tmp_path / "models"
        models.mkdir()
        (models / "users.json").write_text(
            json.dumps({"id": "raw.users", "type": "source", "tags": ["raw"]})
        )
        return tmp_path

    @staticmethod
    def _load(registry: Registry, models_dir: Path) -> None:
        import json as _json

        registry.clear()
        for path in sorted(models_dir.rglob("*.json")):
            data = _json.loads(path.read_text())
            registry.register(Asset.model_validate(data))

    @pytest.fixture
    def manager(self, project_dir: Path) -> StateManager:
        registry = Registry()
        backend = SQLiteBackend.memory()
        env_config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        return StateManager(registry, backend, env_config)

    def test_first_apply_creates_version_1(
        self, manager: StateManager, project_dir: Path
    ) -> None:
        self._load(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        state = manager.backend.load("production")
        assert state is not None
        assert state.assets["raw.users"].version == 1

    def test_second_apply_increments_to_version_2(
        self, manager: StateManager, project_dir: Path
    ) -> None:
        # Create
        self._load(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Modify file
        users_path = project_dir / "models" / "users.json"
        users_path.write_text(
            json.dumps({"id": "raw.users", "type": "source_v2", "tags": ["raw"]})
        )

        # Update
        self._load(manager.registry, project_dir / "models")
        plan2 = manager.plan(environment="production")
        assert plan2.has_changes
        manager.apply(plan2, environment="production")

        state = manager.backend.load("production")
        assert state is not None
        assert state.assets["raw.users"].version == 2

    def test_third_apply_increments_to_version_3(
        self, manager: StateManager, project_dir: Path
    ) -> None:
        """Version increments across three apply cycles: create->update->update."""
        users_path = project_dir / "models" / "users.json"

        # Create (v1)
        self._load(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")
        state = manager.backend.load("production")
        assert state is not None
        assert state.assets["raw.users"].version == 1

        # Update (v2)
        users_path.write_text(
            json.dumps({"id": "raw.users", "type": "source_v2", "tags": ["raw"]})
        )
        self._load(manager.registry, project_dir / "models")
        plan2 = manager.plan(environment="production")
        assert plan2.has_changes
        manager.apply(plan2, environment="production")
        state = manager.backend.load("production")
        assert state is not None
        assert state.assets["raw.users"].version == 2

        # Update again (v3)
        users_path.write_text(
            json.dumps({"id": "raw.users", "type": "source_v3", "tags": ["raw"]})
        )
        self._load(manager.registry, project_dir / "models")
        plan3 = manager.plan(environment="production")
        assert plan3.has_changes
        manager.apply(plan3, environment="production")
        state = manager.backend.load("production")
        assert state is not None
        assert state.assets["raw.users"].version == 3


class TestResolveStateEmptyDependencies:
    """A shallow child env with dependencies=[] should keep empty list."""

    def test_empty_deps_not_overridden_by_parent(self) -> None:
        """When child has dependencies=[] (empty, not None), it should NOT
        fall through to parent's dependencies.
        """
        backend = SQLiteBackend.memory()
        registry = Registry()

        parent_deps = [
            DependencyState(
                source="staging.users",
                target="raw.users",
                fingerprint="depfp1",
            )
        ]
        # Parent has dependencies
        backend.save(
            "production",
            StateSnapshot(
                environment="production",
                assets={"a": AssetState(id="a", fingerprint="fp1")},
                dependencies=parent_deps,
            ),
        )
        # Child explicitly has EMPTY dependencies
        backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                dependencies=[],  # explicitly empty — NOT None
            ),
        )

        env_config = EnvironmentConfig(
            default="dev",
            environments={
                "production": Environment(name="production"),
                "dev": Environment(name="dev", parent="production", shallow=True),
            },
        )
        mgr = StateManager(registry, backend, env_config)
        resolved = mgr._resolve_state(env_config.environments["dev"])

        # Should be empty — child's explicit [] wins over parent's deps
        assert resolved.dependencies == []

    def test_none_deps_inherits_from_parent(self) -> None:
        """When child has no state at all (deps is None), parent deps are inherited."""
        backend = SQLiteBackend.memory()
        registry = Registry()

        parent_deps = [
            DependencyState(
                source="staging.users",
                target="raw.users",
                fingerprint="depfp1",
            )
        ]
        backend.save(
            "production",
            StateSnapshot(
                environment="production",
                assets={"a": AssetState(id="a", fingerprint="fp1")},
                dependencies=parent_deps,
            ),
        )

        env_config = EnvironmentConfig(
            default="dev",
            environments={
                "production": Environment(name="production"),
                "dev": Environment(name="dev", parent="production", shallow=True),
            },
        )
        mgr = StateManager(registry, backend, env_config)

        # No child state at all -> inherits parent fully
        resolved = mgr._resolve_state(env_config.environments["dev"])
        assert len(resolved.dependencies) == 1
        assert resolved.dependencies[0].source == "staging.users"
