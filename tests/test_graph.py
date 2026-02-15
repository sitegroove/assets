"""Tests for AssetGraph."""

import pytest

from assets import Asset, Dependency
from assets.core.graph import AssetGraph


def _build_graph(assets: dict[str, Asset], deps: list[Dependency]) -> AssetGraph:
    return AssetGraph.build(assets, deps)


class TestAssetGraph:
    def test_build_empty(self):
        g = _build_graph({}, [])
        assert len(g) == 0

    def test_build_with_assets(self):
        assets = {
            "a": Asset(id="a"),
            "b": Asset(id="b"),
        }
        g = _build_graph(assets, [Dependency(source="a", target="b")])
        assert len(g) == 2
        assert "a" in g
        assert "b" in g

    def test_ancestors(self, populated_registry):
        g = populated_registry.graph
        assert g.ancestors("mart.enriched") == {
            "staging.users",
            "staging.payments",
            "raw.users",
            "raw.payments",
        }

    def test_descendants(self, populated_registry):
        g = populated_registry.graph
        assert g.descendants("raw.users") == {"staging.users", "mart.enriched"}

    def test_ancestors_with_depth(self, populated_registry):
        g = populated_registry.graph
        # depth=1 means only direct parents
        assert g.ancestors("mart.enriched", max_depth=1) == {
            "staging.users",
            "staging.payments",
        }

    def test_descendants_with_depth(self, populated_registry):
        g = populated_registry.graph
        assert g.descendants("raw.users", max_depth=1) == {"staging.users"}

    def test_roots(self, populated_registry):
        g = populated_registry.graph
        assert g.roots() == {"raw.users", "raw.payments"}

    def test_leaves(self, populated_registry):
        g = populated_registry.graph
        assert g.leaves() == {"mart.enriched"}

    def test_topological_sort(self, populated_registry):
        g = populated_registry.graph
        order = g.topological_sort()
        assert len(order) == 5
        # raw assets must come before staging
        assert order.index("raw.users") < order.index("staging.users")
        assert order.index("raw.payments") < order.index("staging.payments")
        # staging must come before mart
        assert order.index("staging.users") < order.index("mart.enriched")
        assert order.index("staging.payments") < order.index("mart.enriched")

    def test_topological_sort_cycle_detection(self):
        assets = {
            "a": Asset(id="a"),
            "b": Asset(id="b"),
        }
        deps = [
            Dependency(source="a", target="b"),
            Dependency(source="b", target="a"),
        ]
        g = _build_graph(assets, deps)
        with pytest.raises(ValueError, match="Cycle detected"):
            g.topological_sort()

    def test_fingerprint(self, populated_registry):
        g = populated_registry.graph
        fp = g.fingerprint
        assert isinstance(fp, str)
        assert len(fp) == 64  # SHA-256 hex

    def test_fingerprint_changes(self):
        a1 = {"a": Asset(id="a")}
        a2 = {"a": Asset(id="a"), "b": Asset(id="b")}
        g1 = _build_graph(a1, [])
        g2 = _build_graph(a2, [])
        assert g1.fingerprint != g2.fingerprint

    def test_contains(self):
        g = _build_graph({"x": Asset(id="x")}, [])
        assert "x" in g
        assert "y" not in g

    def test_triangle_cycle_detection(self):
        assets = {
            "a": Asset(id="a"),
            "b": Asset(id="b"),
            "c": Asset(id="c"),
        }
        deps = [
            Dependency(source="a", target="b"),
            Dependency(source="b", target="c"),
            Dependency(source="c", target="a"),
        ]
        g = _build_graph(assets, deps)
        with pytest.raises(ValueError, match="Cycle detected"):
            g.topological_sort()

    def test_self_referential_cycle_detection(self):
        assets = {"a": Asset(id="a")}
        deps = [Dependency(source="a", target="a")]
        g = _build_graph(assets, deps)
        with pytest.raises(ValueError, match="Cycle detected"):
            g.topological_sort()

    def test_fingerprint_is_cached(self):
        assets = {"a": Asset(id="a"), "b": Asset(id="b")}
        g = _build_graph(assets, [Dependency(source="a", target="b")])
        fp1 = g.fingerprint
        fp2 = g.fingerprint
        assert fp1 == fp2
        assert fp1 is fp2  # same object — confirms caching

    def test_ancestors_nonexistent_node(self):
        g = _build_graph({"a": Asset(id="a")}, [])
        assert g.ancestors("nonexistent") == set()

    def test_descendants_nonexistent_node(self):
        g = _build_graph({"a": Asset(id="a")}, [])
        assert g.descendants("nonexistent") == set()

    def test_orphaned_dependency_ignored(self):
        # Dependency referencing asset not in graph
        assets = {"a": Asset(id="a")}
        deps = [Dependency(source="a", target="missing")]
        g = _build_graph(assets, deps)
        order = g.topological_sort()
        assert order == ["a"]

    def test_roots_and_leaves_single_node(self):
        g = _build_graph({"a": Asset(id="a")}, [])
        assert g.roots() == {"a"}
        assert g.leaves() == {"a"}

    def test_getitem_returns_asset(self):
        g = _build_graph({"a": Asset(id="a")}, [])
        assert g["a"].id == "a"

    def test_getitem_missing_raises_helpful_error(self):
        assets = {name: Asset(id=name) for name in ["a", "b", "c", "d", "e", "f"]}
        g = _build_graph(assets, [])

        with pytest.raises(KeyError) as err:
            _ = g["missing"]

        message = str(err.value)
        assert "Asset 'missing' not found in graph" in message
        assert "Available:" in message

    def test_to_dict_exports_assets_and_dependencies(self):
        assets = {
            "raw.users": Asset(id="raw.users", type="source"),
            "staging.users": Asset(id="staging.users", type="model"),
        }
        deps = [Dependency(source="raw.users", target="staging.users", type="ref")]
        g = _build_graph(assets, deps)

        payload = g.to_dict()
        assert set(payload.keys()) == {"assets", "dependencies"}
        assert len(payload["assets"]) == 2
        assert payload["dependencies"] == [
            {"source": "raw.users", "target": "staging.users", "type": "ref"}
        ]

    def test_to_mermaid_sanitizes_ids(self):
        assets = {
            "raw.users": Asset(id="raw.users"),
            "staging-users": Asset(id="staging-users"),
            "mart sales": Asset(id="mart sales"),
        }
        deps = [
            Dependency(source="raw.users", target="staging-users"),
            Dependency(source="staging-users", target="mart sales"),
        ]
        g = _build_graph(assets, deps)

        mermaid = g.to_mermaid()
        assert mermaid.startswith("graph LR")
        assert 'raw_users["raw.users"]' in mermaid
        assert 'staging_users["staging-users"]' in mermaid
        assert 'mart_sales["mart sales"]' in mermaid
        assert "raw_users --> staging_users" in mermaid
        assert "staging_users --> mart_sales" in mermaid


class TestStale:
    """Tests for AssetGraph.stale() — topology-aware cascade."""

    def test_stale_single_root(self):
        """Changed root cascades to all descendants."""
        assets = {
            "a": Asset(id="a"),
            "b": Asset(id="b"),
            "c": Asset(id="c"),
        }
        deps = [
            Dependency(source="a", target="b"),
            Dependency(source="b", target="c"),
        ]
        g = _build_graph(assets, deps)
        result = g.stale({"a"})
        assert result == {"a", "b", "c"}

    def test_stale_leaf_no_cascade(self):
        """Changed leaf has no descendants — only itself."""
        assets = {
            "a": Asset(id="a"),
            "b": Asset(id="b"),
        }
        deps = [Dependency(source="a", target="b")]
        g = _build_graph(assets, deps)
        result = g.stale({"b"})
        assert result == {"b"}

    def test_stale_middle_node(self):
        """Changed middle node cascades to downstream only."""
        assets = {
            "a": Asset(id="a"),
            "b": Asset(id="b"),
            "c": Asset(id="c"),
        }
        deps = [
            Dependency(source="a", target="b"),
            Dependency(source="b", target="c"),
        ]
        g = _build_graph(assets, deps)
        result = g.stale({"b"})
        assert result == {"b", "c"}

    def test_stale_multiple_changed(self):
        """Multiple changed assets union their cascades."""
        assets = {
            "a": Asset(id="a"),
            "b": Asset(id="b"),
            "c": Asset(id="c"),
            "d": Asset(id="d"),
        }
        deps = [
            Dependency(source="a", target="b"),
            Dependency(source="c", target="d"),
        ]
        g = _build_graph(assets, deps)
        result = g.stale({"a", "c"})
        assert result == {"a", "b", "c", "d"}

    def test_stale_empty_set(self):
        """No changes means no stale assets."""
        assets = {"a": Asset(id="a")}
        g = _build_graph(assets, [])
        result = g.stale(set())
        assert result == set()

    def test_stale_unknown_name_included_but_no_traverse(self):
        """Names not in graph appear in result but don't cascade."""
        assets = {"a": Asset(id="a")}
        g = _build_graph(assets, [])
        result = g.stale({"deleted_asset"})
        assert result == {"deleted_asset"}

    def test_stale_diamond(self):
        """Diamond dependency: a -> b, a -> c, b -> d, c -> d."""
        assets = {
            "a": Asset(id="a"),
            "b": Asset(id="b"),
            "c": Asset(id="c"),
            "d": Asset(id="d"),
        }
        deps = [
            Dependency(source="a", target="b"),
            Dependency(source="a", target="c"),
            Dependency(source="b", target="d"),
            Dependency(source="c", target="d"),
        ]
        g = _build_graph(assets, deps)
        result = g.stale({"a"})
        assert result == {"a", "b", "c", "d"}

    def test_stale_with_populated_registry(self, populated_registry):
        """Integration test with the standard populated_registry fixture."""
        g = populated_registry.graph
        result = g.stale({"raw.users"})
        assert result == {"raw.users", "staging.users", "mart.enriched"}
