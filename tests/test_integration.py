"""Integration tests — end-to-end workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from assets import (
    Asset,
    AssetField,
    Environment,
    EnvironmentConfig,
    GraphSelector,
    Registry,
    SQLiteBackend,
    StateManager,
)


# ── Consumer models for integration tests ────────────────────


class Column(Asset):
    type: str = ""


class DataModel(Asset):
    sql: str | None = None
    columns: list[Column] = cast(
        list[Column],
        AssetField(default_factory=list, children=True),
    )
    row_count: int = cast(int, AssetField(default=0, fingerprint=False))


# ── Helpers ──────────────────────────────────────────────────


def _load_json_assets(
    registry: Registry,
    models_dir: Path,
    asset_class: type[Asset] = DataModel,
) -> None:
    """Load JSON asset files into the registry."""
    for path in sorted(models_dir.rglob("*.json")):
        data = json.loads(path.read_text())
        registry.register(asset_class.model_validate(data))


@pytest.fixture
def full_project(tmp_path: Path) -> Path:
    models = tmp_path / "models"
    models.mkdir()

    (models / "raw_users.json").write_text(
        json.dumps(
            {
                "id": "raw.users",
                "type": "source",
                "tags": ["raw"],
                "columns": [
                    {"id": "user_id", "type": "column"},
                    {"id": "email", "type": "column"},
                ],
            }
        )
    )

    (models / "raw_payments.json").write_text(
        json.dumps(
            {
                "id": "raw.payments",
                "type": "source",
                "tags": ["raw"],
                "columns": [
                    {"id": "payment_id", "type": "column"},
                    {"id": "user_id", "type": "column"},
                    {"id": "amount", "type": "column"},
                ],
            }
        )
    )

    (models / "staging_users.json").write_text(
        json.dumps(
            {
                "id": "staging.users",
                "type": "data_model",
                "tags": ["staging", "pii"],
                "sql": (
                    "SELECT u.user_id, LOWER(TRIM(u.email)) AS email_clean"
                    " FROM raw.users u"
                ),
                "depends_on": ["raw.users"],
                "columns": [
                    {"id": "user_id", "type": "column"},
                    {"id": "email_clean", "type": "column"},
                ],
            }
        )
    )

    (models / "mart_enriched.json").write_text(
        json.dumps(
            {
                "id": "mart.enriched",
                "type": "data_model",
                "tags": ["mart"],
                "sql": (
                    "SELECT u.*, p.amount FROM staging.users u"
                    " JOIN raw.payments p ON u.user_id = p.user_id"
                ),
                "depends_on": ["staging.users", "raw.payments"],
                "columns": [
                    {"id": "user_id", "type": "column"},
                    {"id": "email_clean", "type": "column"},
                    {"id": "amount", "type": "column"},
                ],
            }
        )
    )

    return tmp_path


class TestFullWorkflow:
    def test_load_plan_apply_cycle(self, full_project: Path):
        registry = Registry()
        backend = SQLiteBackend.memory()
        config = EnvironmentConfig(
            default="production",
            environments={
                "production": Environment(name="production"),
            },
        )
        mgr = StateManager(registry, backend, config)

        # Load and plan
        _load_json_assets(registry, full_project / "models")
        plan = mgr.plan(environment="production")
        assert plan.has_changes
        assert len(plan.changeset.asset_changes) == 4

        # Apply
        result = mgr.apply(plan, environment="production")
        assert result.created == 4

        # No changes after apply
        registry.clear()
        _load_json_assets(registry, full_project / "models")
        plan2 = mgr.plan(environment="production")
        assert not plan2.has_changes

    def test_graph_queries(self, full_project: Path):
        registry = Registry()
        _load_json_assets(registry, full_project / "models")

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
        _load_json_assets(registry, full_project / "models")

        # Tag selector
        selector = GraphSelector(registry)
        pii = selector.execute("tag:pii")
        assert pii.names == {"staging.users"}

        # Kind selector
        sources = selector.execute("type:source")
        assert sources.names == {"raw.users", "raw.payments"}

        # Wildcard
        raw = selector.execute("raw.*")
        assert raw.names == {"raw.users", "raw.payments"}

        # Graph expansion
        downstream = selector.execute("raw.users+")
        assert "staging.users" in downstream.names
        assert "mart.enriched" in downstream.names

    def test_children_introspection(self, full_project: Path):
        registry = Registry()
        _load_json_assets(registry, full_project / "models")

        asset = registry.get("staging.users")
        assert asset is not None

        # children() returns Pydantic objects
        kids = asset.children()
        kid_ids = [c.id for c in kids]
        assert "user_id" in kid_ids
        assert "email_clean" in kid_ids

        # child() with namespaced path
        col = asset.child("columns/email_clean")
        assert col is not None
        assert col.type == "column"

    def test_fingerprint_stability(self, full_project: Path):
        registry1 = Registry()
        _load_json_assets(registry1, full_project / "models")

        registry2 = Registry()
        _load_json_assets(registry2, full_project / "models")

        for name in ["raw.users", "staging.users", "mart.enriched"]:
            a1 = registry1.get(name)
            a2 = registry2.get(name)
            assert a1 is not None and a2 is not None
            assert a1.fingerprint == a2.fingerprint

    def test_multi_env_workflow(self, full_project: Path):
        registry = Registry()
        backend = SQLiteBackend.memory()
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
        mgr = StateManager(registry, backend, config)

        # Apply to production
        _load_json_assets(registry, full_project / "models")
        plan = mgr.plan(environment="production")
        mgr.apply(plan, environment="production")

        # Dev should inherit (no changes)
        registry.clear()
        _load_json_assets(registry, full_project / "models")
        dev_plan = mgr.plan(environment="development")
        assert not dev_plan.has_changes

        # Promote production → staging
        promote = mgr.promote_to("staging", from_env="production")
        assert promote.has_changes
        mgr.apply(promote, environment="staging")

        # No more promotion needed
        promote2 = mgr.promote_to("staging", from_env="production")
        assert not promote2.has_changes

    def test_row_count_not_fingerprinted(self, full_project: Path):
        """row_count changes should NOT trigger a plan change."""
        registry = Registry()
        backend = SQLiteBackend.memory()
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        mgr = StateManager(registry, backend, config)

        # Apply
        _load_json_assets(registry, full_project / "models")
        plan = mgr.plan(environment="production")
        mgr.apply(plan, environment="production")

        # Modify row_count in file — should NOT cause changes since
        # row_count is fingerprint=False. The file content changed but
        # the fingerprint (which excludes row_count) stays the same.
        p = full_project / "models" / "raw_users.json"
        data = json.loads(p.read_text())
        data["row_count"] = 999
        p.write_text(json.dumps(data))

        registry.clear()
        _load_json_assets(registry, full_project / "models")
        plan2 = mgr.plan(environment="production")
        # The plan may detect a file change but the fingerprint should match,
        # so no actual asset changes.
        assert not plan2.has_changes
