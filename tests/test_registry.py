"""Tests for the Registry."""

import pytest

from assets import Asset, FieldMapping, Registry
from assets.resolver.lineage import DependencyResolver


class CountingResolver(DependencyResolver):
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, asset: Asset, schema: dict[str, list[str]]) -> list[FieldMapping]:
        self.calls += 1
        if not schema:
            return []
        source = next(iter(schema))
        return [
            FieldMapping(
                source=f"{source}/id",
                target=f"{asset.id}/id",
            )
        ]


class TestRegistry:
    def test_register_and_get(self, registry: Registry):
        a = Asset(id="test")
        registry.register(a)
        assert registry.get("test") is a

    def test_get_missing(self, registry: Registry):
        assert registry.get("nonexistent") is None

    def test_all(self, registry: Registry):
        a1 = Asset(id="a")
        a2 = Asset(id="b")
        registry.register(a1)
        registry.register(a2)
        assert len(registry.all()) == 2

    def test_clear(self, registry: Registry):
        registry.register(Asset(id="a"))
        registry.clear()
        assert registry.get("a") is None
        assert len(registry.all()) == 0

    def test_register_uses_depends_on(self, registry: Registry):
        a = Asset(
            id="staging.users",
            sql="SELECT * FROM raw.users",
            depends_on=["raw.users"],
        )
        registry.register(a)
        assert a.depends_on == ["raw.users"]

    def test_register_creates_dependencies(self, registry: Registry):
        a = Asset(
            id="staging.users",
            sql="SELECT * FROM raw.users",
            depends_on=["raw.users"],
        )
        registry.register(a)
        assert len(registry.dependencies) == 1
        dep = registry.dependencies[0]
        assert dep.source == "raw.users"
        assert dep.target == "staging.users"

    def test_register_no_deps(self, registry: Registry):
        a = Asset(id="raw.users")
        registry.register(a)
        assert a.depends_on == []
        assert len(registry.dependencies) == 0

    def test_graph_lazy_build(self, populated_registry: Registry):
        g = populated_registry.graph
        assert len(g) == 5

    def test_graph_invalidates_on_register(self, populated_registry: Registry):
        g1 = populated_registry.graph
        populated_registry.register(Asset(id="new"))
        g2 = populated_registry.graph
        assert g1 is not g2

    def test_select(self, populated_registry: Registry):
        result = populated_registry.select("tag:pii")
        assert "staging.users" in result.names

    def test_register_overwrite(self, registry: Registry):
        a1 = Asset(id="test", type="v1")
        a2 = Asset(id="test", type="v2")
        registry.register(a1)
        registry.register(a2)
        assert registry.get("test").type == "v2"  # type: ignore[union-attr]

    def test_resolve_field_dependency_no_resolver_raises(self, registry: Registry):
        registry.register(Asset(id="test", sql="SELECT 1"))
        with pytest.raises(ValueError, match="resolver instance must be provided"):
            registry.resolve_field_dependency(asset_id="test")

    def test_resolve_unknown_resolver_raises(self, registry: Registry):
        with pytest.raises(ValueError, match="Unknown resolver"):
            registry.resolve("lineage", asset_id="x")

    def test_add_resolver_duplicate_raises(self, registry: Registry):
        resolver = CountingResolver()
        registry.add_resolver("lineage", resolver)
        with pytest.raises(ValueError, match="already registered"):
            registry.add_resolver("lineage", resolver)

    def test_resolve_by_asset_id(self, registry: Registry):
        resolver = CountingResolver()
        registry.add_resolver("lineage", resolver)
        registry.register(
            Asset(
                id="raw.users",
                type="source",
                children=[Asset(id="id", type="column")],
            )
        )
        registry.register(
            Asset(
                id="staging.users",
                sql="SELECT id FROM raw.users",
                depends_on=["raw.users"],
            )
        )

        result = registry.resolve("lineage", asset_id="staging.users")
        assert resolver.calls == 1
        assert len(result) == 1
        assert result[0].source == "raw.users/id"
        assert result[0].target == "staging.users/id"

    def test_resolve_by_selector(self, registry: Registry):
        resolver = CountingResolver()
        registry.add_resolver("lineage", resolver)
        registry.register(Asset(id="raw.users", children=[Asset(id="id")]))
        registry.register(
            Asset(
                id="staging.users",
                type="data_model",
                sql="SELECT id FROM raw.users",
                depends_on=["raw.users"],
            )
        )
        registry.register(
            Asset(
                id="mart.users",
                type="data_model",
                sql="SELECT id FROM staging.users",
                depends_on=["staging.users"],
            )
        )

        result = registry.resolve("lineage", selector="type:data_model")
        assert resolver.calls == 2
        assert len(result) == 2

    def test_resolve_uses_cache_when_inputs_unchanged(self, registry: Registry):
        resolver = CountingResolver()
        registry.add_resolver("lineage", resolver)
        registry.register(Asset(id="raw.users", children=[Asset(id="id")]))
        registry.register(
            Asset(
                id="staging.users",
                sql="SELECT id FROM raw.users",
                depends_on=["raw.users"],
            )
        )

        first = registry.resolve("lineage", asset_id="staging.users")
        second = registry.resolve("lineage", asset_id="staging.users")
        assert resolver.calls == 1
        assert first == second

    def test_resolve_cache_invalidates_after_upstream_change(self, registry: Registry):
        resolver = CountingResolver()
        registry.add_resolver("lineage", resolver)
        registry.register(Asset(id="raw.users", children=[Asset(id="id")]))
        registry.register(
            Asset(
                id="staging.users",
                sql="SELECT id FROM raw.users",
                depends_on=["raw.users"],
            )
        )

        registry.resolve("lineage", asset_id="staging.users")
        registry.register(
            Asset(
                id="raw.users",
                children=[Asset(id="id"), Asset(id="email")],
            )
        )
        registry.resolve("lineage", asset_id="staging.users")
        assert resolver.calls == 2

    def test_reregister_deduplicates_dependencies(self, registry: Registry):
        a = Asset(
            id="staging.users",
            sql="SELECT * FROM raw.users",
            depends_on=["raw.users"],
        )
        registry.register(a)
        assert len(registry.dependencies) == 1
        # Re-register same asset — should NOT accumulate duplicates
        registry.register(a)
        assert len(registry.dependencies) == 1

    def test_reregister_updates_dependencies(self, registry: Registry):
        a1 = Asset(
            id="staging.users",
            sql="SELECT * FROM raw.users",
            depends_on=["raw.users"],
        )
        registry.register(a1)
        assert registry.dependencies[0].source == "raw.users"

        # Re-register with different dependency
        a2 = Asset(
            id="staging.users",
            sql="SELECT * FROM raw.orders",
            depends_on=["raw.orders"],
        )
        registry.register(a2)
        assert len(registry.dependencies) == 1
        assert registry.dependencies[0].source == "raw.orders"

    def test_reregister_no_deps_clears_dependencies(self, registry: Registry):
        a1 = Asset(
            id="x",
            sql="SELECT * FROM y",
            depends_on=["y"],
        )
        registry.register(a1)
        assert len(registry.dependencies) == 1

        a2 = Asset(id="x")  # no deps
        registry.register(a2)
        assert len(registry.dependencies) == 0

    def test_duplicate_depends_on_deduplicated(self, registry: Registry):
        a = Asset(
            id="y",
            sql="SELECT * FROM x JOIN x ON 1=1",
            depends_on=["x", "x"],
        )
        registry.register(a)
        assert len(registry.dependencies) == 1

    def test_resolve_field_dependency_no_target_raises(self, registry: Registry):
        class StubResolver(DependencyResolver):
            def resolve(
                self,
                asset: Asset,
                schema: dict[str, list[str]],
            ) -> list[FieldMapping]:
                return []

        with pytest.raises(ValueError, match="Provide either"):
            registry.resolve_field_dependency(resolver=StubResolver())

    # ── register_many ────────────────────────────────────────

    def test_register_many_basic(self, registry: Registry):
        assets = [Asset(id="a"), Asset(id="b"), Asset(id="c")]
        registry.register_many(assets)
        assert len(registry) == 3
        assert registry.get("a") is assets[0]
        assert registry.get("b") is assets[1]
        assert registry.get("c") is assets[2]

    def test_register_many_builds_dependencies(self, registry: Registry):
        assets = [
            Asset(id="raw.users", type="source"),
            Asset(
                id="staging.users",
                sql="SELECT * FROM raw.users",
                depends_on=["raw.users"],
            ),
        ]
        registry.register_many(assets)
        assert len(registry.dependencies) == 1
        assert registry.dependencies[0].source == "raw.users"
        assert registry.dependencies[0].target == "staging.users"

    def test_register_many_invalidates_graph_once(self, registry: Registry):
        registry.register(Asset(id="x"))
        g1 = registry.graph
        registry.register_many([Asset(id="a"), Asset(id="b")])
        g2 = registry.graph
        assert g1 is not g2

    def test_register_many_overwrites_existing(self, registry: Registry):
        registry.register(Asset(id="a", type="v1"))
        registry.register_many([Asset(id="a", type="v2")])
        assert registry.get("a").type == "v2"  # type: ignore[union-attr]

    def test_register_many_deduplicates_deps(self, registry: Registry):
        assets = [
            Asset(id="a", depends_on=["x"]),
            Asset(id="b", depends_on=["x"]),
        ]
        registry.register_many(assets)
        # Should have 2 deps: x->a, x->b
        assert len(registry.dependencies) == 2

    def test_register_many_clears_old_deps_on_reregister(self, registry: Registry):
        registry.register(Asset(id="a", depends_on=["x"]))
        assert len(registry.dependencies) == 1
        assert registry.dependencies[0].source == "x"

        registry.register_many([Asset(id="a", depends_on=["y"])])
        assert len(registry.dependencies) == 1
        assert registry.dependencies[0].source == "y"

    def test_register_many_empty_list(self, registry: Registry):
        registry.register(Asset(id="existing"))
        registry.register_many([])
        assert len(registry) == 1

    # ── unregister ───────────────────────────────────────────

    def test_unregister_removes_asset(self, registry: Registry):
        registry.register(Asset(id="a"))
        assert registry.unregister("a") is True
        assert registry.get("a") is None
        assert len(registry) == 0

    def test_unregister_missing_returns_false(self, registry: Registry):
        assert registry.unregister("nonexistent") is False

    def test_unregister_removes_outgoing_deps(self, registry: Registry):
        registry.register(Asset(id="a", depends_on=["b"]))
        registry.register(Asset(id="b"))
        assert len(registry.dependencies) == 1

        registry.unregister("a")
        assert len(registry.dependencies) == 0

    def test_unregister_removes_incoming_deps(self, registry: Registry):
        registry.register(Asset(id="a"))
        registry.register(Asset(id="b", depends_on=["a"]))
        assert len(registry.dependencies) == 1

        registry.unregister("a")
        # Dependency referencing 'a' as source should be removed
        assert len(registry.dependencies) == 0

    def test_unregister_invalidates_graph(self, registry: Registry):
        registry.register(Asset(id="a"))
        g1 = registry.graph
        registry.unregister("a")
        g2 = registry.graph
        assert g1 is not g2

    # ── unregister_many ──────────────────────────────────────

    def test_unregister_many_basic(self, registry: Registry):
        registry.register(Asset(id="a"))
        registry.register(Asset(id="b"))
        registry.register(Asset(id="c"))
        removed = registry.unregister_many(["a", "c"])
        assert removed == 2
        assert registry.get("a") is None
        assert registry.get("b") is not None
        assert registry.get("c") is None

    def test_unregister_many_partial(self, registry: Registry):
        registry.register(Asset(id="a"))
        removed = registry.unregister_many(["a", "nonexistent"])
        assert removed == 1

    def test_unregister_many_empty(self, registry: Registry):
        registry.register(Asset(id="a"))
        removed = registry.unregister_many([])
        assert removed == 0
        assert len(registry) == 1

    def test_unregister_many_removes_deps(self, registry: Registry):
        registry.register(Asset(id="a"))
        registry.register(Asset(id="b", depends_on=["a"]))
        registry.register(Asset(id="c", depends_on=["b"]))
        assert len(registry.dependencies) == 2

        registry.unregister_many(["a", "b"])
        assert len(registry.dependencies) == 0

    def test_unregister_many_invalidates_graph(self, registry: Registry):
        registry.register(Asset(id="a"))
        registry.register(Asset(id="b"))
        g1 = registry.graph
        registry.unregister_many(["a"])
        g2 = registry.graph
        assert g1 is not g2
