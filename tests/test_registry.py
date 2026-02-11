"""Tests for the Registry."""

import pytest

from assets import Asset, Registry


class TestRegistry:
    def test_register_and_get(self, registry: Registry):
        a = Asset(name="test")
        registry.register(a)
        assert registry.get("test") is a

    def test_get_missing(self, registry: Registry):
        assert registry.get("nonexistent") is None

    def test_all(self, registry: Registry):
        a1 = Asset(name="a")
        a2 = Asset(name="b")
        registry.register(a1)
        registry.register(a2)
        assert len(registry.all()) == 2

    def test_clear(self, registry: Registry):
        registry.register(Asset(name="a"))
        registry.clear()
        assert registry.get("a") is None
        assert len(registry.all()) == 0

    def test_register_extracts_refs(self, registry: Registry):
        a = Asset(name="staging.users", sql="SELECT * FROM {{ ref('raw.users') }}")
        registry.register(a)
        assert a.depends_on == ["raw.users"]

    def test_register_creates_dependencies(self, registry: Registry):
        a = Asset(name="staging.users", sql="SELECT * FROM {{ ref('raw.users') }}")
        registry.register(a)
        assert len(registry.dependencies) == 1
        dep = registry.dependencies[0]
        assert dep.source == "raw.users"
        assert dep.target == "staging.users"

    def test_register_no_sql_no_deps(self, registry: Registry):
        a = Asset(name="raw.users")
        registry.register(a)
        assert a.depends_on == []
        assert len(registry.dependencies) == 0

    def test_graph_lazy_build(self, populated_registry: Registry):
        g = populated_registry.graph
        assert len(g) == 5

    def test_graph_invalidates_on_register(self, populated_registry: Registry):
        g1 = populated_registry.graph
        populated_registry.register(Asset(name="new"))
        g2 = populated_registry.graph
        assert g1 is not g2

    def test_select(self, populated_registry: Registry):
        result = populated_registry.select("tag:pii")
        assert "staging.users" in result.names

    def test_register_overwrite(self, registry: Registry):
        a1 = Asset(name="test", kind="v1")
        a2 = Asset(name="test", kind="v2")
        registry.register(a1)
        registry.register(a2)
        assert registry.get("test").kind == "v2"  # type: ignore[union-attr]

    def test_resolve_column_lineage_no_resolver_raises(self, registry: Registry):
        registry.register(Asset(name="test", sql="SELECT 1"))
        with pytest.raises(ValueError, match="resolver instance must be provided"):
            registry.resolve_column_lineage(asset_name="test")


class TestRegisterBulk:
    def test_basic(self, registry: Registry):
        assets = [Asset(name="a"), Asset(name="b"), Asset(name="c")]
        registry.register_bulk(assets)
        assert len(registry.all()) == 3
        assert registry.get("a") is not None

    def test_with_precomputed_refs(self, registry: Registry):
        a1 = Asset(name="raw.users")
        a2 = Asset(name="staging.users", sql="SELECT * FROM {{ ref('raw.users') }}")
        registry.register_bulk(
            [a1, a2],
            precomputed_refs=[[], ["raw.users"]],
        )
        assert a2.depends_on == ["raw.users"]
        assert len(registry.dependencies) == 1
        assert registry.dependencies[0].source == "raw.users"

    def test_without_precomputed_refs(self, registry: Registry):
        """When precomputed_refs is None, refs are extracted from SQL."""
        a1 = Asset(name="raw.users")
        a2 = Asset(name="staging.users", sql="SELECT * FROM {{ ref('raw.users') }}")
        registry.register_bulk([a1, a2])
        assert a2.depends_on == ["raw.users"]

    def test_mixed_precomputed_refs(self, registry: Registry):
        """Some entries have precomputed refs, others need extraction."""
        a1 = Asset(name="raw.users")
        a2 = Asset(name="staging.users", sql="SELECT * FROM {{ ref('raw.users') }}")
        registry.register_bulk(
            [a1, a2],
            precomputed_refs=[[], None],  # None → extract from SQL
        )
        assert a2.depends_on == ["raw.users"]

    def test_cycle_detection(self, registry: Registry):
        """Bulk registration detects cycles via topological sort."""
        a = Asset(name="a", sql="SELECT * FROM {{ ref('b') }}")
        b = Asset(name="b", sql="SELECT * FROM {{ ref('a') }}")
        with pytest.raises(ValueError, match="[Cc]ycle"):
            registry.register_bulk([a, b])

    def test_preserves_existing(self, registry: Registry):
        """Bulk registration preserves assets registered with register()."""
        registry.register(Asset(name="existing"))
        registry.register_bulk([Asset(name="new1"), Asset(name="new2")])
        assert registry.get("existing") is not None
        assert len(registry.all()) == 3

    def test_replaces_existing(self, registry: Registry):
        """Bulk registration overwrites assets with the same name."""
        registry.register(Asset(name="test", kind="v1"))
        registry.register_bulk([Asset(name="test", kind="v2")])
        assert registry.get("test").kind == "v2"  # type: ignore[union-attr]

    def test_stale_deps_cleaned(self, registry: Registry):
        """Re-registering an asset via bulk cleans its old dependencies."""
        a1 = Asset(name="raw.users")
        a2 = Asset(name="staging.users", sql="SELECT * FROM {{ ref('raw.users') }}")
        registry.register_bulk([a1, a2])
        assert len(registry.dependencies) == 1

        # Re-register staging.users without deps
        a3 = Asset(name="staging.users", sql=None)
        registry.register_bulk([a3], precomputed_refs=[[]])
        deps_to_staging = [d for d in registry.dependencies if d.target == "staging.users"]
        assert len(deps_to_staging) == 0

    def test_graph_built_after_bulk(self, registry: Registry):
        """Graph is available after bulk registration."""
        a1 = Asset(name="raw.users")
        a2 = Asset(name="staging.users", sql="SELECT * FROM {{ ref('raw.users') }}")
        registry.register_bulk([a1, a2])
        g = registry.graph
        assert len(g) == 2
        assert "raw.users" in g.ancestors("staging.users")
