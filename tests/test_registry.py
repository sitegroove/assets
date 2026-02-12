"""Tests for the Registry."""

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

    def test_resolve_field_dependency_no_resolver_raises(self, registry: Registry):
        import pytest

        registry.register(Asset(name="test", sql="SELECT 1"))
        with pytest.raises(ValueError, match="resolver instance must be provided"):
            registry.resolve_field_dependency(asset_name="test")
