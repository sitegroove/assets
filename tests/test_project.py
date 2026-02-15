"""Tests for high-level Project facade."""

from __future__ import annotations

import pytest

from assets import Asset, DependencyResolver, FieldMapping, Project, SQLiteBackend


class DataModel(Asset):
    """Subclass with sql for project tests."""

    sql: str | None = None


class PassthroughResolver(DependencyResolver):
    def resolve(
        self,
        asset: Asset,
        schema: dict[str, list[str]],
    ) -> list[FieldMapping]:
        if not schema:
            return []
        first_source = next(iter(schema.keys()))
        first_field = schema[first_source][0] if schema[first_source] else "id"
        return [
            FieldMapping(
                source=f"{first_source}/{first_field}",
                target=f"{asset.id}/id",
            )
        ]


class TestAssetsFacade:
    def test_constructor_validation(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="Provide only one"):
            Project(
                state_dir=str(tmp_path / ".assets_state"),
                backend=SQLiteBackend.memory(),
            )

    def test_registry_only_mode(self) -> None:
        project = Project()
        project.register(Asset(id="raw.users", type="source", tags=["raw"]))

        result = project.select("tag:raw")
        assert result.names == {"raw.users"}
        assert len(project) == 1
        assert "raw.users" in project

        with pytest.raises(RuntimeError, match="No state backend configured"):
            project.plan()

        # state: selectors without a manager return empty with a warning
        result = project.select("state:modified")
        assert result.names == set()
        assert any("requires a StateManager" in w for w in result.warnings)

    def test_select_with_exclude(self) -> None:
        project = Project()
        project.register(Asset(id="raw.users", type="source", tags=["raw"]))
        project.register(Asset(id="raw.events", type="source", tags=["raw"]))
        project.register(
            Asset(id="staging.users", type="data_model", tags=["staging"]),
        )

        result = project.select("type:source", exclude="raw.events")
        assert result.names == {"raw.users"}

    def test_plan_apply_with_default_environment(self, tmp_path) -> None:
        project = Project(state_dir=str(tmp_path / ".assets_state"))
        project.register(Asset(id="raw.users", type="source", tags=["raw"]))

        plan = project.plan()
        assert plan.environment == "default"
        assert plan.has_changes

        apply_result = project.apply(plan)
        assert apply_result.created == 1

        plan2 = project.plan()
        assert not plan2.has_changes

        # Property and wrapper coverage
        assert project.manager is not None
        assert project.registry.get("raw.users") is not None
        assert project.graph is not None
        assert project.get("raw.users") is not None
        assert project.all()[0].id == "raw.users"
        assert project.unregister("raw.users") is True
        assert project.unregister("raw.users") is False

    def test_env_config_override_and_repr(self, tmp_path) -> None:
        from assets import Environment, EnvironmentConfig

        env_config = EnvironmentConfig(
            default="default",
            environments={"default": Environment(name="default")},
            protected={"production"},
        )
        project = Project(
            state_dir=str(tmp_path / ".assets_state"),
            env_config=env_config,
            protected_environments={"default"},
        )
        assert project.manager is not None
        assert project.manager.env_config.protected == {"default"}
        assert "state_enabled=True" in repr(project)

    def test_select_state_uses_active_environment(self, tmp_path) -> None:
        project = Project(state_dir=str(tmp_path / ".assets_state"))
        project.register_many(
            [
                Asset(id="raw.users", type="source", tags=["raw"]),
                DataModel(
                    id="staging.users",
                    type="data_model",
                    tags=["staging", "pii"],
                    sql="SELECT * FROM raw.users",
                    depends_on=["raw.users"],
                ),
            ]
        )
        project.apply(project.plan())

        project.clear()
        project.register_many(
            [
                Asset(id="raw.users", type="source", tags=["raw"]),
                DataModel(
                    id="staging.users",
                    type="data_model",
                    tags=["staging", "pii"],
                    sql="SELECT user_id FROM raw.users",
                    depends_on=["raw.users"],
                ),
            ]
        )

        updated = project.select("state:updated")
        modified_and_pii = project.select("state:modified,tag:pii")

        assert updated.names == {"staging.users"}
        assert modified_and_pii.names == {"staging.users"}

    def test_promote_to_uses_active_environment(self, tmp_path) -> None:
        project = Project(
            environment="production",
            state_dir=str(tmp_path / ".assets_state"),
        )
        project.register(Asset(id="raw.users", type="source", tags=["raw"]))
        project.apply(project.plan())

        promote_plan = project.promote_to("staging")
        assert promote_plan.environment == "staging"
        assert promote_plan.has_changes

        drift = project.drift()
        assert not drift.has_changes

    def test_protected_environments_are_configurable(self, tmp_path) -> None:
        project = Project(
            state_dir=str(tmp_path / ".assets_state"),
            protected_environments={"default", "production"},
        )
        project.create_environment("tmp")
        project.destroy_environment("tmp")

        with pytest.raises(ValueError, match="protected"):
            project.destroy_environment("default")

    def test_resolver_export_and_load_wrappers(self, tmp_path) -> None:
        project = Project(state_dir=str(tmp_path / ".assets_state"))
        project.register_many(
            [
                Asset(id="raw.users", type="source", tags=["raw"]),
                Asset(
                    id="staging.users",
                    type="data_model",
                    depends_on=["raw.users"],
                ),
            ]
        )

        project.add_resolver("lineage", PassthroughResolver())
        mappings = project.resolve("lineage", asset_id="staging.users")
        assert len(mappings) == 1

        export_dict = project.to_dict()
        export_mermaid = project.to_mermaid()
        assert "assets" in export_dict
        assert "graph LR" in export_mermaid

        load_result = project.load(groups=[])
        assert load_result.loaded == 0

        project.clear()
        assert len(project) == 0
