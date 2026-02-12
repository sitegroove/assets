"""Tests for AssetGraph."""

import pytest

from assets import Asset, Dependency
from assets.core.graph import AssetGraph


def _build_graph(
    assets: dict[str, Asset], deps: list[Dependency]
) -> AssetGraph:
    return AssetGraph.build(assets, deps)


class TestAssetGraph:
    def test_build_empty(self):
        g = _build_graph({}, [])
        assert len(g) == 0

    def test_build_with_assets(self):
        assets = {
            "a": Asset(name="a"),
            "b": Asset(name="b"),
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
            "a": Asset(name="a"),
            "b": Asset(name="b"),
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
        a1 = {"a": Asset(name="a")}
        a2 = {"a": Asset(name="a"), "b": Asset(name="b")}
        g1 = _build_graph(a1, [])
        g2 = _build_graph(a2, [])
        assert g1.fingerprint != g2.fingerprint

    def test_contains(self):
        g = _build_graph({"x": Asset(name="x")}, [])
        assert "x" in g
        assert "y" not in g

    def test_edge_types_stored(self):
        assets = {"a": Asset(name="a"), "b": Asset(name="b")}
        deps = [Dependency(source="a", target="b", type="ref")]
        g = _build_graph(assets, deps)
        assert g._edge_types[("a", "b")] == "ref"

    def test_children_containment_edges(self):
        assets = {
            "t": Asset(name="t"),
            "t/c1": Asset(name="t/c1", parent="t"),
            "t/c2": Asset(name="t/c2", parent="t"),
            "other": Asset(name="other"),
        }
        deps = [
            Dependency(source="t", target="t/c1", type="contains"),
            Dependency(source="t", target="t/c2", type="contains"),
            Dependency(source="t", target="other", type="ref"),
        ]
        g = _build_graph(assets, deps)
        assert g.children("t") == {"t/c1", "t/c2"}

    def test_data_dependencies_excludes_containment(self):
        assets = {
            "t": Asset(name="t"),
            "t/c1": Asset(name="t/c1", parent="t"),
            "other": Asset(name="other"),
        }
        deps = [
            Dependency(source="t", target="t/c1", type="contains"),
            Dependency(source="t", target="other", type="ref"),
        ]
        g = _build_graph(assets, deps)
        assert g.data_dependencies("t") == {"other"}

    def test_top_level_assets(self):
        assets = {
            "a": Asset(name="a"),
            "a/x": Asset(name="a/x", parent="a"),
            "b": Asset(name="b"),
        }
        g = _build_graph(assets, [])
        top = g.top_level_assets()
        assert set(top.keys()) == {"a", "b"}

    def test_children_empty(self):
        g = _build_graph({"a": Asset(name="a")}, [])
        assert g.children("a") == set()
