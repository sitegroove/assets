"""Tests for UX improvements: factory methods, __repr__, error handling."""

import json
from pathlib import Path

from assets import (
    ApplyResult,
    Environment,
    EnvironmentConfig,
    MemoryBackend,
    Plan,
    Registry,
    StateManager,
)


class TestStateManagerFromDir:
    def test_from_dir_defaults(self, tmp_path: Path):
        models = tmp_path / "models"
        models.mkdir()
        (models / "test.json").write_text(
            json.dumps({"name": "test.asset", "kind": "source"})
        )

        manager = StateManager.from_dir(str(models), backend=MemoryBackend())
        assert isinstance(manager, StateManager)
        plan = manager.plan(str(models))
        assert plan.has_changes

    def test_from_dir_with_environments(self, tmp_path: Path):
        models = tmp_path / "models"
        models.mkdir()
        (models / "a.json").write_text(
            json.dumps({"name": "a", "kind": "source"})
        )

        envs = {
            "production": Environment(name="production"),
            "staging": Environment(name="staging", parent="production"),
        }
        manager = StateManager.from_dir(
            str(models),
            backend=MemoryBackend(),
            environments=envs,
            default_env="production",
        )
        plan = manager.plan(str(models), environment="production")
        assert plan.has_changes
        assert plan.environment == "production"

    def test_from_dir_with_string_backend_creates_fsspec(self, tmp_path: Path):
        models = tmp_path / "models"
        models.mkdir()
        (models / "a.json").write_text(
            json.dumps({"name": "a", "kind": "source"})
        )

        state_dir = tmp_path / "state"
        manager = StateManager.from_dir(
            str(models),
            backend=str(state_dir),
        )
        # Should have created a FsspecBackend
        from assets.state.fsspec import FsspecBackend

        assert isinstance(manager.backend, FsspecBackend)

    def test_from_dir_plan_apply_cycle(self, tmp_path: Path):
        models = tmp_path / "models"
        models.mkdir()
        (models / "users.json").write_text(
            json.dumps({"name": "raw.users", "kind": "source"})
        )

        manager = StateManager.from_dir(str(models), backend=MemoryBackend())
        plan = manager.plan(str(models))
        result = manager.apply(plan)
        assert result.created == 1

        # No changes after apply
        plan2 = manager.plan(str(models))
        assert not plan2.has_changes


class TestReprMethods:
    def test_plan_repr(self):
        plan = Plan(environment="prod")
        assert repr(plan) == "Plan(environment='prod', changes=0)"

    def test_plan_repr_with_changes(self):
        from assets.engine.differ import Change, ChangeSet

        cs = ChangeSet(asset_changes=[
            Change(action="create", asset_name="a"),
            Change(action="update", asset_name="b"),
        ])
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
        from assets import Asset

        registry = Registry()
        assert repr(registry) == "Registry(assets=0, dependencies=0)"

        registry.register(Asset(name="a"))
        assert repr(registry) == "Registry(assets=1, dependencies=0)"

    def test_registry_len(self):
        from assets import Asset

        registry = Registry()
        assert len(registry) == 0
        registry.register(Asset(name="a"))
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

    def test_get_default(self):
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        env = config.get()
        assert env.name == "production"
