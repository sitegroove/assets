"""Integration tests — end-to-end workflows."""

import json
from pathlib import Path

import pytest

from assets import (
    Asset,
    AssetField,
    Environment,
    EnvironmentConfig,
    MemoryBackend,
    ProjectLoader,
    Registry,
    StateManager,
)


class DataModel(Asset):
    row_count: int = AssetField(default=0, fingerprint=False)


@pytest.fixture
def full_project(tmp_path: Path) -> Path:
    models = tmp_path / "models"
    models.mkdir()

    (models / "raw_users.json").write_text(
        json.dumps({
            "name": "raw.users",
            "kind": "source",
            "tags": ["raw"],
            "children": [
                {"name": "user_id", "kind": "column"},
                {"name": "email", "kind": "column"},
            ],
        })
    )

    (models / "raw_payments.json").write_text(
        json.dumps({
            "name": "raw.payments",
            "kind": "source",
            "tags": ["raw"],
            "children": [
                {"name": "payment_id", "kind": "column"},
                {"name": "user_id", "kind": "column"},
                {"name": "amount", "kind": "column"},
            ],
        })
    )

    (models / "staging_users.json").write_text(
        json.dumps({
            "name": "staging.users",
            "kind": "data_model",
            "tags": ["staging", "pii"],
            "sql": (
                "SELECT u.user_id, LOWER(TRIM(u.email)) AS email_clean"
                " FROM {{ ref('raw.users') }} u"
            ),
            "children": [
                {"name": "user_id", "kind": "column"},
                {"name": "email_clean", "kind": "column"},
            ],
        })
    )

    (models / "mart_enriched.json").write_text(
        json.dumps({
            "name": "mart.enriched",
            "kind": "data_model",
            "tags": ["mart"],
            "sql": (
                "SELECT u.*, p.amount FROM {{ ref('staging.users') }} u"
                " JOIN {{ ref('raw.payments') }} p ON u.user_id = p.user_id"
            ),
            "children": [
                {"name": "user_id", "kind": "column"},
                {"name": "email_clean", "kind": "column"},
                {"name": "amount", "kind": "column"},
            ],
        })
    )

    return tmp_path


class TestFullWorkflow:
    def test_load_plan_apply_cycle(self, full_project: Path):
        registry = Registry()
        loader = ProjectLoader(
            registry,
            asset_class=DataModel,
            cache_dir=str(full_project / ".cache"),
        )
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={
                "production": Environment(name="production"),
            },
        )
        mgr = StateManager(registry, loader, backend, config)

        # Plan
        plan = mgr.plan(str(full_project / "models"), environment="production")
        assert plan.has_changes
        assert len(plan.changeset.asset_changes) == 4

        # Apply
        result = mgr.apply(plan, environment="production")
        assert result.created == 4

        # No changes after apply
        plan2 = mgr.plan(str(full_project / "models"), environment="production")
        assert not plan2.has_changes

    def test_graph_queries(self, full_project: Path):
        registry = Registry()
        loader = ProjectLoader(
            registry,
            asset_class=DataModel,
            cache_dir=str(full_project / ".cache"),
        )
        loader.load(str(full_project / "models"))

        # Graph traversal
        g = registry.graph
        assert g.ancestors("mart.enriched") == {
            "staging.users",
            "raw.payments",
            "raw.users",
        }
        assert g.descendants("raw.users") == {"staging.users", "mart.enriched"}
        assert g.roots() == {"raw.users", "raw.payments"}
        assert g.leaves() == {"mart.enriched"}

        # Topological sort
        order = g.topological_sort()
        assert order.index("raw.users") < order.index("staging.users")
        assert order.index("staging.users") < order.index("mart.enriched")

    def test_selector_queries(self, full_project: Path):
        registry = Registry()
        loader = ProjectLoader(
            registry,
            asset_class=DataModel,
            cache_dir=str(full_project / ".cache"),
        )
        loader.load(str(full_project / "models"))

        # Tag selector
        pii = registry.select("tag:pii")
        assert pii.names == {"staging.users"}

        # Kind selector
        sources = registry.select("kind:source")
        assert sources.names == {"raw.users", "raw.payments"}

        # Wildcard
        raw = registry.select("raw.*")
        assert raw.names == {"raw.users", "raw.payments"}

        # Graph expansion
        downstream = registry.select("raw.users+")
        assert "staging.users" in downstream.names
        assert "mart.enriched" in downstream.names

    def test_children_introspection(self, full_project: Path):
        registry = Registry()
        loader = ProjectLoader(
            registry,
            asset_class=DataModel,
            cache_dir=str(full_project / ".cache"),
        )
        loader.load(str(full_project / "models"))

        asset = registry.get("staging.users")
        assert asset is not None

        # list_children
        children = asset.list_children()
        assert "user_id" in children
        assert "email_clean" in children

        # get_child
        col = asset.get_child("email_clean")
        assert col is not None
        assert col.kind == "column"

    def test_fingerprint_stability(self, full_project: Path):
        registry1 = Registry()
        loader1 = ProjectLoader(
            registry1,
            asset_class=DataModel,
            cache_dir=str(full_project / ".cache1"),
        )
        loader1.load(str(full_project / "models"))

        registry2 = Registry()
        loader2 = ProjectLoader(
            registry2,
            asset_class=DataModel,
            cache_dir=str(full_project / ".cache2"),
        )
        loader2.load(str(full_project / "models"))

        for name in ["raw.users", "staging.users", "mart.enriched"]:
            a1 = registry1.get(name)
            a2 = registry2.get(name)
            assert a1 is not None and a2 is not None
            assert a1.fingerprint == a2.fingerprint

    def test_multi_env_workflow(self, full_project: Path):
        registry = Registry()
        loader = ProjectLoader(
            registry,
            asset_class=DataModel,
            cache_dir=str(full_project / ".cache"),
        )
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="development",
            environments={
                "production": Environment(name="production"),
                "staging": Environment(name="staging", parent="production"),
                "development": Environment(
                    name="development", parent="production", shallow=True
                ),
            },
        )
        mgr = StateManager(registry, loader, backend, config)

        # Apply to production
        plan = mgr.plan(str(full_project / "models"), environment="production")
        mgr.apply(plan, environment="production")

        # Dev should inherit (no changes)
        dev_plan = mgr.plan(str(full_project / "models"), environment="development")
        assert not dev_plan.has_changes

        # Promote production → staging
        promote = mgr.promote(from_env="production", to_env="staging")
        assert promote.has_changes
        mgr.apply(promote, environment="staging")

        # No more promotion needed
        promote2 = mgr.promote(from_env="production", to_env="staging")
        assert not promote2.has_changes

    def test_row_count_not_fingerprinted(self, full_project: Path):
        """row_count changes should NOT trigger a plan change."""
        registry = Registry()
        loader = ProjectLoader(
            registry,
            asset_class=DataModel,
            cache_dir=str(full_project / ".cache"),
        )
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        mgr = StateManager(registry, loader, backend, config)

        # Apply
        plan = mgr.plan(str(full_project / "models"), environment="production")
        mgr.apply(plan, environment="production")

        # Modify row_count in file — should NOT cause changes since
        # row_count is fingerprint=False. The file content changed but
        # the fingerprint (which excludes row_count) stays the same.
        p = full_project / "models" / "raw_users.json"
        data = json.loads(p.read_text())
        data["row_count"] = 999
        p.write_text(json.dumps(data))

        plan2 = mgr.plan(str(full_project / "models"), environment="production")
        # The plan may detect a file change but the fingerprint should match,
        # so no actual asset changes.
        assert not plan2.has_changes
