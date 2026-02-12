"""Tests for the Registry."""

from assets import Asset, AssetField, Registry


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
        import pytest

        registry.register(Asset(name="test", sql="SELECT 1"))
        with pytest.raises(ValueError, match="resolver instance must be provided"):
            registry.resolve_column_lineage(asset_name="test")


class Column(Asset):
    type: str = ""
    pii: bool = False


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)


class TestRegistryChildren:
    def test_inline_children_flattened(self, registry: Registry):
        m = DataModel(
            name="staging.users",
            columns=[Column(name="id", type="INT"), Column(name="email", type="VARCHAR")],
        )
        registry.register(m)
        assert registry.get("staging.users/id") is not None
        assert registry.get("staging.users/email") is not None

    def test_child_name_qualified(self, registry: Registry):
        m = DataModel(
            name="staging.users",
            columns=[Column(name="email")],
        )
        registry.register(m)
        child = registry.get("staging.users/email")
        assert child is not None
        assert child.name == "staging.users/email"

    def test_child_parent_set(self, registry: Registry):
        m = DataModel(
            name="staging.users",
            columns=[Column(name="email")],
        )
        registry.register(m)
        child = registry.get("staging.users/email")
        assert child is not None
        assert child.parent == "staging.users"

    def test_child_kind_default(self, registry: Registry):
        m = DataModel(
            name="staging.users",
            columns=[Column(name="email")],
        )
        registry.register(m)
        child = registry.get("staging.users/email")
        assert child is not None
        assert child.kind == "field"

    def test_containment_dependency_created(self, registry: Registry):
        m = DataModel(
            name="staging.users",
            columns=[Column(name="email")],
        )
        registry.register(m)
        contains_deps = [
            d for d in registry.dependencies if d.type == "contains"
        ]
        assert len(contains_deps) == 1
        assert contains_deps[0].source == "staging.users"
        assert contains_deps[0].target == "staging.users/email"

    def test_explicit_child_registration(self, registry: Registry):
        registry.register(Asset(name="staging.users", kind="data_model"))
        registry.register(
            Asset(name="staging.users/email", kind="column", parent="staging.users")
        )
        assert registry.get("staging.users/email") is not None
        contains_deps = [
            d for d in registry.dependencies if d.type == "contains"
        ]
        assert len(contains_deps) == 1

    def test_children_method(self, registry: Registry):
        m = DataModel(
            name="staging.users",
            columns=[Column(name="id"), Column(name="email")],
        )
        registry.register(m)
        children = registry.children("staging.users")
        names = {c.name for c in children}
        assert names == {"staging.users/id", "staging.users/email"}

    def test_children_empty(self, registry: Registry):
        registry.register(Asset(name="raw.users"))
        assert registry.children("raw.users") == []

    def test_tree_fingerprint(self, registry: Registry):
        m = DataModel(
            name="staging.users",
            columns=[Column(name="id"), Column(name="email")],
        )
        registry.register(m)
        fp = registry.tree_fingerprint("staging.users")
        assert isinstance(fp, str)
        assert len(fp) == 64  # SHA-256

    def test_tree_fingerprint_changes_with_children(self, registry: Registry):
        r1 = Registry()
        m1 = DataModel(name="t", columns=[Column(name="a")])
        r1.register(m1)

        r2 = Registry()
        m2 = DataModel(name="t", columns=[Column(name="a"), Column(name="b")])
        r2.register(m2)

        assert r1.tree_fingerprint("t") != r2.tree_fingerprint("t")

    def test_tree_fingerprint_missing(self, registry: Registry):
        assert registry.tree_fingerprint("nonexistent") == ""

    def test_total_asset_count_includes_children(self, registry: Registry):
        m = DataModel(
            name="staging.users",
            columns=[Column(name="id"), Column(name="email")],
        )
        registry.register(m)
        # 1 parent + 2 children = 3
        assert len(registry) == 3
