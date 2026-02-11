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
