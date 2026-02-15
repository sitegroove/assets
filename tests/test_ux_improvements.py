"""Tests for UX improvements: factory methods, __repr__, error handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assets import (
    ApplyResult,
    Asset,
    Environment,
    EnvironmentConfig,
    Plan,
    Registry,
    StateManager,
)


def _load_json_assets(
    registry: Registry,
    models_dir: Path,
    asset_class: type[Asset] = Asset,
) -> None:
    """Load JSON asset files into the registry."""
    for path in sorted(models_dir.rglob("*.json")):
        data = json.loads(path.read_text())
        registry.register(asset_class.model_validate(data))


class TestStateManagerCreate:
    def test_create_with_local_path(self, tmp_path: Path):
        models = tmp_path / "models"
        models.mkdir()
        (models / "test.json").write_text(
            json.dumps({"id": "test.asset", "type": "source"})
        )

        registry = Registry()
        _load_json_assets(registry, models)
        manager = StateManager.create(
            registry, local_path=str(tmp_path / ".assets_state")
        )
        assert isinstance(manager, StateManager)
        plan = manager.plan(environment="production")
        assert plan.has_changes

    def test_create_with_environments(self, tmp_path: Path):
        models = tmp_path / "models"
        models.mkdir()
        (models / "a.json").write_text(json.dumps({"id": "a", "type": "source"}))

        envs = {
            "production": Environment(name="production"),
            "staging": Environment(name="staging", parent="production"),
        }
        registry = Registry()
        _load_json_assets(registry, models)
        manager = StateManager.create(
            registry,
            local_path=str(tmp_path / ".assets_state"),
            environments=envs,
            default_env="production",
        )
        plan = manager.plan(environment="production")
        assert plan.has_changes
        assert plan.environment == "production"

    def test_create_plan_apply_cycle(self, tmp_path: Path):
        models = tmp_path / "models"
        models.mkdir()
        (models / "users.json").write_text(
            json.dumps({"id": "raw.users", "type": "source"})
        )

        registry = Registry()
        _load_json_assets(registry, models)
        manager = StateManager.create(
            registry, local_path=str(tmp_path / ".assets_state")
        )
        plan = manager.plan(environment="production")
        result = manager.apply(plan)
        assert result.created == 1

        # No changes after apply
        registry.clear()
        _load_json_assets(registry, models)
        plan2 = manager.plan(environment="production")
        assert not plan2.has_changes

    def test_create_allows_configurable_protected_environments(self, tmp_path: Path):
        registry = Registry()
        manager = StateManager.create(
            registry,
            local_path=str(tmp_path / ".assets_state"),
            environments={"default": Environment(name="default")},
            default_env="default",
            protected_environments={"default"},
        )

        with pytest.raises(ValueError, match="protected"):
            manager.destroy_environment("default")


class TestReprMethods:
    def test_plan_repr(self):
        plan = Plan(environment="prod")
        assert repr(plan) == "Plan(environment='prod', changes=0)"

    def test_plan_repr_with_changes(self):
        from assets.engine.differ import Change, ChangeSet

        cs = ChangeSet(
            asset_changes=[
                Change(action="create", asset_id="a"),
                Change(action="update", asset_id="b"),
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        assert repr(plan) == "Plan(environment='dev', changes=2)"

    def test_apply_result_repr(self):
        result = ApplyResult(
            applied=3, created=1, updated=1, deleted=1, environment="prod"
        )
        assert "created=1" in repr(result)
        assert "updated=1" in repr(result)
        assert "deleted=1" in repr(result)
        assert "prod" in repr(result)

    def test_registry_repr(self):
        registry = Registry()
        assert repr(registry) == "Registry(assets=0, dependencies=0)"

        registry.register(Asset(id="a"))
        assert repr(registry) == "Registry(assets=1, dependencies=0)"

    def test_registry_len(self):
        registry = Registry()
        assert len(registry) == 0
        registry.register(Asset(id="a"))
        assert len(registry) == 1


class TestEnvironmentConfigBehavior:
    def test_get_known_environment(self):
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        env = config.get("production")
        assert env.name == "production"

    def test_get_unknown_creates_adhoc(self):
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        # Ad-hoc environment for PR branch
        env = config.get("pr-142")
        assert env.name == "pr-142"
        assert env.parent is None
        assert env.shallow is False

    def test_get_unknown_raises_when_implicit_disabled(self):
        import pytest

        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
            allow_implicit_environments=False,
        )
        with pytest.raises(ValueError, match="not configured"):
            config.get("pr-142")

    def test_get_default(self):
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        env = config.get()
        assert env.name == "production"


class TestApplyValidationBehavior:
    def test_apply_empty_plan_validates_environment(self, tmp_path: Path):
        registry = Registry()
        manager = StateManager.create(
            registry,
            local_path=str(tmp_path / ".assets_state"),
            environments={"production": Environment(name="production")},
            default_env="production",
        )
        manager.env_config.allow_implicit_environments = False

        with pytest.raises(ValueError, match="not configured"):
            manager.apply(Plan(environment="ghost"))
