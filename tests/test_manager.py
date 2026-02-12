"""Tests for StateManager."""

import json
from pathlib import Path

import pytest

from assets import (
    Environment,
    EnvironmentConfig,
    MemoryBackend,
    ProjectLoader,
    Registry,
    StateManager,
)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    models = tmp_path / "models"
    models.mkdir()
    (models / "users.json").write_text(
        json.dumps({"name": "raw.users", "kind": "source", "tags": ["raw"]})
    )
    (models / "orders.json").write_text(
        json.dumps({"name": "raw.orders", "kind": "source", "tags": ["raw"]})
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
    loader = ProjectLoader(
        registry, cache_dir=str(project_dir / ".cache")
    )
    backend = MemoryBackend()
    return StateManager(registry, loader, backend, env_config)


class TestStateManager:
    def test_plan_first_run(self, manager: StateManager, project_dir: Path):
        plan = manager.plan(
            str(project_dir / "models"), environment="production"
        )
        assert plan.has_changes
        creates = [c for c in plan.changeset.asset_changes if c.action == "create"]
        assert len(creates) == 2

    def test_plan_no_changes_after_apply(self, manager: StateManager, project_dir: Path):
        plan = manager.plan(
            str(project_dir / "models"), environment="production"
        )
        manager.apply(plan, environment="production")

        plan2 = manager.plan(
            str(project_dir / "models"), environment="production"
        )
        assert not plan2.has_changes

    def test_apply_result(self, manager: StateManager, project_dir: Path):
        plan = manager.plan(
            str(project_dir / "models"), environment="production"
        )
        result = manager.apply(plan, environment="production")
        assert result.created == 2
        assert result.applied == 2

    def test_plan_detects_update(self, manager: StateManager, project_dir: Path):
        # First apply
        plan = manager.plan(str(project_dir / "models"), environment="production")
        manager.apply(plan, environment="production")

        # Modify a file
        (project_dir / "models" / "users.json").write_text(
            json.dumps({"name": "raw.users", "kind": "source_v2", "tags": ["raw"]})
        )

        plan2 = manager.plan(str(project_dir / "models"), environment="production")
        assert plan2.has_changes
        updates = [c for c in plan2.changeset.asset_changes if c.action == "update"]
        assert len(updates) == 1
        assert updates[0].asset_name == "raw.users"

    def test_plan_detects_delete(self, manager: StateManager, project_dir: Path):
        plan = manager.plan(str(project_dir / "models"), environment="production")
        manager.apply(plan, environment="production")

        # Delete a file
        (project_dir / "models" / "orders.json").unlink()

        plan2 = manager.plan(str(project_dir / "models"), environment="production")
        assert plan2.has_changes
        deletes = [c for c in plan2.changeset.asset_changes if c.action == "delete"]
        assert len(deletes) == 1
        assert deletes[0].asset_name == "raw.orders"

    def test_plan_with_selector(self, manager: StateManager, project_dir: Path):
        plan = manager.plan(
            str(project_dir / "models"),
            environment="production",
            selector="raw.users",
        )
        creates = [c for c in plan.changeset.asset_changes if c.action == "create"]
        assert len(creates) == 1
        assert creates[0].asset_name == "raw.users"

    def test_shallow_env_inherits_parent(self, manager: StateManager, project_dir: Path):
        # Apply to production
        plan = manager.plan(str(project_dir / "models"), environment="production")
        manager.apply(plan, environment="production")

        # Plan for shallow dev — should see no changes since parent has everything
        plan_dev = manager.plan(str(project_dir / "models"), environment="development")
        assert not plan_dev.has_changes

    def test_promote(self, manager: StateManager, project_dir: Path):
        # Apply to production
        plan = manager.plan(str(project_dir / "models"), environment="production")
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
        plan = manager.plan(str(project_dir / "models"), environment="production")
        manager.apply(plan, environment="production")

        # No drift initially
        drift = manager.drift(str(project_dir / "models"), environment="production")
        assert not drift.has_changes

    def test_apply_empty_changeset_is_noop(self, manager: StateManager, project_dir: Path):
        plan = manager.plan(str(project_dir / "models"), environment="production")
        manager.apply(plan, environment="production")

        # Second plan has no changes
        plan2 = manager.plan(str(project_dir / "models"), environment="production")
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

    def test_circular_parent_env_raises(self):
        from assets import (
            Environment, EnvironmentConfig, MemoryBackend,
            ProjectLoader, Registry, StateManager,
        )

        config = EnvironmentConfig(
            default="a",
            environments={
                "a": Environment(name="a", parent="b", shallow=True),
                "b": Environment(name="b", parent="a", shallow=True),
            },
        )
        mgr = StateManager(
            Registry(),
            ProjectLoader(Registry()),
            MemoryBackend(),
            config,
        )
        with pytest.raises(ValueError, match="Circular parent reference"):
            mgr._resolve_state(config.environments["a"])
