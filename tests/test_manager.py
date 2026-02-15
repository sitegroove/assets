"""Tests for StateManager."""

from __future__ import annotations

import json
from contextlib import contextmanager
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
from assets.state.backend import StateBackend
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
        default="production",
        environments={
            "production": Environment(name="production"),
            "staging": Environment(name="staging"),
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

    def test_apply_delete_removes_from_state(
        self, manager: StateManager, project_dir: Path
    ):
        """After applying a delete, the asset is fully removed from state."""
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Remove orders from registry
        (project_dir / "models" / "orders.json").unlink()
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")

        delete_plan = manager.plan(environment="production")
        manager.apply(delete_plan, environment="production")

        state = manager.backend.load("production")
        assert state is not None
        assert "raw.orders" not in state.assets
        assert "raw.users" in state.assets

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

    def test_promote(self, manager: StateManager, project_dir: Path):
        # Apply to production
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Promote production -> staging
        promote_plan = manager.promote_to("staging", from_env="production")
        assert promote_plan.has_changes
        manager.apply(promote_plan, environment="staging")

        # Promote again — no changes
        promote_plan2 = manager.promote_to("staging", from_env="production")
        assert not promote_plan2.has_changes

    def test_promote_missing_source_env(self, manager: StateManager):
        # Source env has no state yet — should return empty plan
        plan = manager.promote_to("staging", from_env="production")
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

        plan = manager.promote_to(
            "staging",
            selector="+mart.enriched",
            from_env="production",
        )
        selected = {c.asset_id for c in plan.changeset.asset_changes}
        assert selected == {"raw.users", "staging.users", "mart.enriched"}

    def test_create_environment(self, manager: StateManager):
        env = manager.create_environment("pr-123", parent="production")
        assert env.name == "pr-123"
        assert "pr-123" in manager.env_config.environments

    def test_create_environment_copies_parent_state(
        self, manager: StateManager, project_dir: Path
    ):
        """Creating an env from a parent copies the parent's state."""
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Create dev from production
        manager.create_environment("dev", parent="production")

        # dev should have the same assets as production
        dev_state = manager.backend.load("dev")
        prod_state = manager.backend.load("production")
        assert dev_state is not None
        assert prod_state is not None
        assert set(dev_state.assets.keys()) == set(prod_state.assets.keys())

    def test_create_environment_is_independent(
        self, manager: StateManager, project_dir: Path
    ):
        """After creation, parent and child environments are independent."""
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Create dev from production
        manager.create_environment("dev", parent="production")

        # Apply a change only in dev: add a new asset
        manager.registry.register(Asset(id="dev.only", type="test"))
        dev_plan = manager.plan(environment="dev")
        manager.apply(dev_plan, environment="dev")

        # dev should have the new asset, production should not
        dev_state = manager.backend.load("dev")
        prod_state = manager.backend.load("production")
        assert dev_state is not None
        assert prod_state is not None
        assert "dev.only" in dev_state.assets
        assert "dev.only" not in prod_state.assets

    def test_create_environment_from_empty_parent(self, manager: StateManager):
        """Creating an env from a parent with no state yields empty state."""
        manager.create_environment("empty-child", parent="production")
        child_state = manager.backend.load("empty-child")
        # Parent had no state, so child has no state either
        assert child_state is None

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

    def test_drift_detects_new_asset(self, manager: StateManager, project_dir: Path):
        """Drift detects assets added to registry after last apply."""
        manager.registry.clear()
        _load_json_assets(manager.registry, project_dir / "models")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Add a new asset to registry
        manager.registry.register(Asset(id="raw.events", type="source"))
        drift = manager.drift(environment="production")
        assert drift.has_changes
        creates = [c for c in drift.changeset.asset_changes if c.action == "create"]
        assert len(creates) == 1
        assert creates[0].asset_id == "raw.events"

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

        # Apply should be a noop
        result = manager.apply(plan2, environment="production")
        assert result.applied == 0
        assert result.created == 0


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


class DummyBackend(StateBackend):
    """Minimal backend used to exercise StateManager index guard rails."""

    def load(self, environment: str) -> StateSnapshot | None:
        return None

    def save(
        self,
        environment: str,
        state: StateSnapshot,
        *,
        changed_ids: set[str] | None = None,
    ) -> None:
        return None

    @contextmanager
    def lock(self, environment: str):
        yield

    def list_environments(self) -> list[str]:
        return []

    def delete_environment(self, environment: str) -> None:
        return None


class TestStateManagerIndexGuards:
    def test_index_raises_for_in_memory_backend(self) -> None:
        manager = StateManager(
            Registry(),
            SQLiteBackend.memory(),
            EnvironmentConfig(
                default="production",
                environments={"production": Environment(name="production")},
            ),
        )

        with pytest.raises(RuntimeError, match="file-backed SQLiteBackend"):
            _ = manager.index

    def test_index_raises_for_unsupported_backend(self) -> None:
        manager = StateManager(
            Registry(),
            DummyBackend(),
            EnvironmentConfig(
                default="production",
                environments={"production": Environment(name="production")},
            ),
        )

        with pytest.raises(RuntimeError, match="requires a SQLiteBackend"):
            _ = manager.index
