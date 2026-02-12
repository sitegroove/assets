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


class Column(Asset):
    type: str = ""
    description: str = ""
    pii: bool = False


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)
    row_count: int = AssetField(default=0, fingerprint=False)


# 4 top-level assets + 10 columns = 14 total
_NUM_TOP_LEVEL = 4
_NUM_COLUMNS = 10
_NUM_TOTAL = _NUM_TOP_LEVEL + _NUM_COLUMNS


@pytest.fixture
def full_project(tmp_path: Path) -> Path:
    models = tmp_path / "models"
    models.mkdir()

    (models / "raw_users.json").write_text(
        json.dumps({
            "name": "raw.users",
            "kind": "source",
            "tags": ["raw"],
            "columns": [
                {"name": "user_id", "type": "INTEGER"},
                {"name": "email", "type": "VARCHAR", "pii": True},
            ],
        })
    )

    (models / "raw_payments.json").write_text(
        json.dumps({
            "name": "raw.payments",
            "kind": "source",
            "tags": ["raw"],
            "columns": [
                {"name": "payment_id", "type": "INTEGER"},
                {"name": "user_id", "type": "INTEGER"},
                {"name": "amount", "type": "DECIMAL"},
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
            "columns": [
                {"name": "user_id", "type": "INTEGER"},
                {"name": "email_clean", "type": "VARCHAR", "pii": True},
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
            "columns": [
                {"name": "user_id", "type": "INTEGER"},
                {"name": "email_clean", "type": "VARCHAR"},
                {"name": "amount", "type": "DECIMAL"},
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

        # Plan — 4 parents + 10 columns = 14 total
        plan = mgr.plan(str(full_project / "models"), environment="production")
        assert plan.has_changes
        assert len(plan.changeset.asset_changes) == _NUM_TOTAL

        # Apply
        result = mgr.apply(plan, environment="production")
        assert result.created == _NUM_TOTAL

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

        g = registry.graph

        # Ancestors traverse backward (ref) edges — unaffected by containment
        assert g.ancestors("mart.enriched") == {
            "staging.users",
            "raw.payments",
            "raw.users",
        }

        # Descendants now include containment children
        desc = g.descendants("raw.users")
        assert {"staging.users", "mart.enriched"}.issubset(desc)
        assert "raw.users/user_id" in desc
        assert "raw.users/email" in desc

        # Roots: assets with no backward edges (unchanged)
        assert g.roots() == {"raw.users", "raw.payments"}

        # Leaves: column children are the new leaves
        leaves = g.leaves()
        assert "mart.enriched" not in leaves  # has children now
        assert "mart.enriched/user_id" in leaves
        assert "mart.enriched/amount" in leaves

        # Top-level assets filter
        top = g.top_level_assets()
        assert set(top.keys()) == {"raw.users", "raw.payments", "staging.users", "mart.enriched"}

        # Topological sort still orders data-flow correctly
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

        # Kind selector — only top-level sources, not children
        sources = registry.select("kind:source")
        assert sources.names == {"raw.users", "raw.payments"}

        # Kind:field — children
        fields = registry.select("kind:field")
        assert "raw.users/user_id" in fields.names
        assert len(fields.names) == _NUM_COLUMNS

        # Top-level wildcard via top: selector
        top_raw = registry.select("top:raw.*")
        assert top_raw.names == {"raw.users", "raw.payments"}

        # Children selector
        children = registry.select("children:raw.users")
        assert children.names == {"raw.users/user_id", "raw.users/email"}

        # Graph expansion
        downstream = registry.select("raw.users+")
        assert "staging.users" in downstream.names
        assert "mart.enriched" in downstream.names

    def test_field_introspection(self, full_project: Path):
        registry = Registry()
        loader = ProjectLoader(
            registry,
            asset_class=DataModel,
            cache_dir=str(full_project / ".cache"),
        )
        loader.load(str(full_project / "models"))

        asset = registry.get("staging.users")
        assert asset is not None

        # list_fields returns local (unqualified) names
        fields = asset.list_fields()
        assert "user_id" in fields
        assert "email_clean" in fields

        # get_field matches by local name
        col = asset.get_field("email_clean")
        assert col is not None
        assert col.pii is True  # type: ignore[attr-defined]

        # Children also accessible via registry
        children = registry.children("staging.users")
        child_names = {c.local_name for c in children}
        assert child_names == {"user_id", "email_clean"}

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

        # Tree fingerprints also stable
        for name in ["raw.users", "staging.users"]:
            assert registry1.tree_fingerprint(name) == registry2.tree_fingerprint(name)

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
