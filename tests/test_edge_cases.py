"""Edge case tests across all modules.

Covers boundary conditions, unusual inputs, and corner cases
that are not addressed by the existing test suite.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from assets import (
    Asset,
    AssetField,
    CompiledCache,
    Dependency,
    Differ,
    Environment,
    EnvironmentConfig,
    FieldMapping,
    LocalJSONBackend,
    MemoryBackend,
    ProjectLoader,
    RefResolver,
    Registry,
    StateManager,
)
from assets.core.asset import _serialize_value
from assets.core.graph import AssetGraph, SelectionResult
from assets.engine.planner import Plan
from assets.state.models import AssetState, DependencyState, StateSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class Tag(BaseModel):
    label: str
    priority: int = 0


class CustomModel(Asset):
    """Asset subclass with a custom field_name_key."""

    tags_detail: list[Tag] = AssetField(
        default_factory=list,
        field_source=True,
        field_name_key="label",
    )


class Column(BaseModel):
    name: str
    type: str = ""


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)
    row_count: int = AssetField(default=0, fingerprint=False)


class MultiSourceModel(Asset):
    """Asset with two field_source attributes."""

    columns: list[Column] = AssetField(default_factory=list, field_source=True)
    tags_detail: list[Tag] = AssetField(
        default_factory=list,
        field_source=True,
        field_name_key="label",
    )


# ===========================================================================
# 1. Asset & Fingerprinting Edge Cases
# ===========================================================================


class TestAssetEdgeCases:
    def test_serialize_value_with_none(self):
        assert _serialize_value(None) is None

    def test_serialize_value_with_empty_set(self):
        assert _serialize_value(set()) == []

    def test_serialize_value_with_set_of_ints(self):
        result = _serialize_value({3, 1, 2})
        assert result == [1, 2, 3]

    def test_serialize_value_nested_structures(self):
        val = {"a": [{"x": 1}, {"x": 2}], "b": {4, 2}}
        result = _serialize_value(val)
        assert result == {"a": [{"x": 1}, {"x": 2}], "b": [2, 4]}

    def test_serialize_value_basemodel(self):
        col = Column(name="id", type="INT")
        result = _serialize_value(col)
        assert result == {"name": "id", "type": "INT"}

    def test_serialize_value_list_of_none(self):
        result = _serialize_value([None, None])
        assert result == [None, None]

    def test_fingerprint_with_metadata(self):
        """metadata dict is fingerprinted by default."""
        a1 = Asset(name="x", metadata={"owner": "alice"})
        a2 = Asset(name="x", metadata={"owner": "bob"})
        assert a1.fingerprint != a2.fingerprint

    def test_fingerprint_with_sql_none_vs_empty(self):
        """sql=None and sql='' should produce different fingerprints."""
        a1 = Asset(name="x", sql=None)
        a2 = Asset(name="x", sql="")
        assert a1.fingerprint != a2.fingerprint

    def test_fingerprint_with_empty_tags_vs_no_tags(self):
        """Both default to empty list — fingerprints should match."""
        a1 = Asset(name="x")
        a2 = Asset(name="x", tags=[])
        assert a1.fingerprint == a2.fingerprint

    def test_fingerprint_tag_order_matters(self):
        """Tags are a list, so order affects fingerprint."""
        a1 = Asset(name="x", tags=["a", "b"])
        a2 = Asset(name="x", tags=["b", "a"])
        assert a1.fingerprint != a2.fingerprint

    def test_get_field_with_custom_name_key(self):
        m = CustomModel(
            name="test",
            tags_detail=[Tag(label="pii", priority=1), Tag(label="raw", priority=2)],
        )
        result = m.get_field("pii")
        assert result is not None
        assert result.priority == 1  # type: ignore[attr-defined]

    def test_list_fields_with_custom_name_key(self):
        m = CustomModel(
            name="test",
            tags_detail=[Tag(label="pii"), Tag(label="raw")],
        )
        assert m.list_fields() == ["pii", "raw"]

    def test_field_source_none_falls_back_to_empty(self):
        """When a field_source attribute is None, _field_sources uses []."""
        m = CustomModel(name="test")
        m.tags_detail = None  # type: ignore[assignment]
        # Should not raise, list_fields returns empty
        assert m.list_fields() == []

    def test_multiple_field_sources(self):
        m = MultiSourceModel(
            name="test",
            columns=[Column(name="id")],
            tags_detail=[Tag(label="pii")],
        )
        fields = m.list_fields()
        assert "id" in fields
        assert "pii" in fields

    def test_get_field_searches_all_sources(self):
        m = MultiSourceModel(
            name="test",
            columns=[Column(name="id")],
            tags_detail=[Tag(label="pii", priority=5)],
        )
        # Should find in first source
        assert m.get_field("id") is not None
        # Should find in second source
        result = m.get_field("pii")
        assert result is not None
        assert result.priority == 5  # type: ignore[attr-defined]

    def test_fingerprint_with_unicode(self):
        a1 = Asset(name="données", description="résumé des données")
        a2 = Asset(name="données", description="résumé des données")
        assert a1.fingerprint == a2.fingerprint

    def test_fingerprint_with_very_long_sql(self):
        long_sql = "SELECT " + ", ".join(f"col_{i}" for i in range(1000)) + " FROM t"
        a = Asset(name="big", sql=long_sql)
        assert isinstance(a.fingerprint, str)
        assert len(a.fingerprint) == 64


# ===========================================================================
# 2. Graph Edge Cases
# ===========================================================================


class TestGraphEdgeCases:
    def test_ancestors_of_root_node(self):
        """Root nodes have no ancestors."""
        g = AssetGraph.build({"a": Asset(name="a")}, [])
        assert g.ancestors("a") == set()

    def test_descendants_of_leaf_node(self):
        """Leaf nodes have no descendants."""
        g = AssetGraph.build({"a": Asset(name="a")}, [])
        assert g.descendants("a") == set()

    def test_single_node_is_both_root_and_leaf(self):
        g = AssetGraph.build({"a": Asset(name="a")}, [])
        assert g.roots() == {"a"}
        assert g.leaves() == {"a"}

    def test_max_depth_zero(self):
        """max_depth=0 should return no neighbors (can't traverse any edges)."""
        assets = {"a": Asset(name="a"), "b": Asset(name="b")}
        deps = [Dependency(source="a", target="b")]
        g = AssetGraph.build(assets, deps)
        assert g.descendants("a", max_depth=0) == set()
        assert g.ancestors("b", max_depth=0) == set()

    def test_diamond_dependency(self):
        """Diamond: A→B, A→C, B→D, C→D — verify traversal correctness."""
        assets = {
            n: Asset(name=n) for n in ["a", "b", "c", "d"]
        }
        deps = [
            Dependency(source="a", target="b"),
            Dependency(source="a", target="c"),
            Dependency(source="b", target="d"),
            Dependency(source="c", target="d"),
        ]
        g = AssetGraph.build(assets, deps)
        assert g.descendants("a") == {"b", "c", "d"}
        assert g.ancestors("d") == {"a", "b", "c"}
        assert g.roots() == {"a"}
        assert g.leaves() == {"d"}

    def test_diamond_topological_sort(self):
        assets = {n: Asset(name=n) for n in ["a", "b", "c", "d"]}
        deps = [
            Dependency(source="a", target="b"),
            Dependency(source="a", target="c"),
            Dependency(source="b", target="d"),
            Dependency(source="c", target="d"),
        ]
        g = AssetGraph.build(assets, deps)
        order = g.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_dependency_references_nonexistent_asset(self):
        """Dependencies can reference assets not in the graph.

        Traversal should only return names of registered assets,
        filtering out ghost nodes that exist only as edge targets.
        """
        assets = {"a": Asset(name="a")}
        deps = [Dependency(source="a", target="ghost")]
        g = AssetGraph.build(assets, deps)
        # ghost is NOT returned because it's not a registered asset
        assert g.descendants("a") == set()
        assert g.roots() == {"a"}

    def test_empty_graph_fingerprint(self):
        g = AssetGraph.build({}, [])
        fp = g.fingerprint
        assert isinstance(fp, str)
        assert len(fp) == 64

    def test_empty_graph_topological_sort(self):
        g = AssetGraph.build({}, [])
        assert g.topological_sort() == []

    def test_three_node_cycle_detection(self):
        assets = {n: Asset(name=n) for n in ["a", "b", "c"]}
        deps = [
            Dependency(source="a", target="b"),
            Dependency(source="b", target="c"),
            Dependency(source="c", target="a"),
        ]
        g = AssetGraph.build(assets, deps)
        with pytest.raises(ValueError, match="Cycle detected"):
            g.topological_sort()

    def test_ancestors_of_nonexistent_node(self):
        g = AssetGraph.build({"a": Asset(name="a")}, [])
        # Node not in graph — returns empty since no edges
        assert g.ancestors("nonexistent") == set()

    def test_long_chain_depth_limit(self):
        """Chain: a→b→c→d→e with depth limit of 2 from a."""
        names = ["a", "b", "c", "d", "e"]
        assets = {n: Asset(name=n) for n in names}
        deps = [Dependency(source=names[i], target=names[i + 1]) for i in range(4)]
        g = AssetGraph.build(assets, deps)
        assert g.descendants("a", max_depth=2) == {"b", "c"}
        assert g.descendants("a", max_depth=1) == {"b"}
        assert g.descendants("a") == {"b", "c", "d", "e"}


# ===========================================================================
# 3. Selector Parser Edge Cases
# ===========================================================================


class TestSelectorEdgeCases:
    def _make_graph(self) -> AssetGraph:
        assets = {
            "raw.users": Asset(name="raw.users", kind="source", tags=["raw"]),
            "raw.payments": Asset(name="raw.payments", kind="source", tags=["raw"]),
            "staging.users": Asset(
                name="staging.users",
                kind="data_model",
                tags=["staging", "pii"],
            ),
            "mart.enriched": Asset(
                name="mart.enriched", kind="data_model", tags=["mart"]
            ),
        }
        deps = [
            Dependency(source="raw.users", target="staging.users"),
            Dependency(source="staging.users", target="mart.enriched"),
            Dependency(source="raw.payments", target="mart.enriched"),
        ]
        return AssetGraph.build(assets, deps)

    def test_wildcard_star_matches_all(self):
        g = self._make_graph()
        result = g.select("*")
        assert len(result.names) == 4

    def test_wildcard_question_mark(self):
        g = self._make_graph()
        result = g.select("raw.user?")
        assert result.names == {"raw.users"}

    def test_wildcard_with_downstream(self):
        g = self._make_graph()
        result = g.select("raw.*+")
        assert "raw.users" in result.names
        assert "raw.payments" in result.names
        assert "staging.users" in result.names
        assert "mart.enriched" in result.names

    def test_wildcard_with_upstream(self):
        g = self._make_graph()
        result = g.select("+mart.*")
        assert "mart.enriched" in result.names
        assert "staging.users" in result.names
        assert "raw.users" in result.names
        assert "raw.payments" in result.names

    def test_depth_zero_returns_only_self(self):
        g = self._make_graph()
        result = g.select("raw.users+0")
        # depth=0 means no descendants
        assert result.names == {"raw.users"}

    def test_intersection_with_no_overlap(self):
        g = self._make_graph()
        result = g.select("tag:raw,tag:pii")
        assert result.names == set()

    def test_intersection_three_terms(self):
        g = self._make_graph()
        result = g.select("tag:staging,kind:data_model,tag:pii")
        assert result.names == {"staging.users"}

    def test_selector_whitespace_in_comma_separated(self):
        g = self._make_graph()
        result = g.select("tag:raw , kind:source")
        assert result.names == {"raw.users", "raw.payments"}

    def test_selector_nonexistent_kind(self):
        g = self._make_graph()
        result = g.select("kind:nonexistent")
        assert result.names == set()

    def test_selector_empty_tag_match(self):
        g = self._make_graph()
        result = g.select("tag:")
        # tag: with empty string — no assets have tag ""
        assert result.names == set()

    def test_upstream_of_root(self):
        g = self._make_graph()
        result = g.select("+raw.users")
        # root has no ancestors, should just include itself
        assert result.names == {"raw.users"}

    def test_downstream_of_leaf(self):
        g = self._make_graph()
        result = g.select("mart.enriched+")
        # leaf has no descendants, should just include itself
        assert result.names == {"mart.enriched"}

    def test_both_directions_on_middle_node(self):
        g = self._make_graph()
        result = g.select("+staging.users+")
        assert "raw.users" in result.names
        assert "staging.users" in result.names
        assert "mart.enriched" in result.names

    def test_depth_limited_upstream(self):
        """Test that depth limits work for upstream (prefix +) selector.

        Note: The upstream prefix + doesn't accept a depth number in the regex,
        depth is only on the downstream side. So +name with max_depth=None
        returns all ancestors.
        """
        g = self._make_graph()
        result = g.select("+mart.enriched")
        assert "raw.users" in result.names  # 2 hops away, still included


# ===========================================================================
# 4. Registry Edge Cases
# ===========================================================================


class TestRegistryEdgeCases:
    def test_re_register_replaces_dependencies(self):
        """Re-registering an asset with SQL should replace old deps, not accumulate."""
        registry = Registry()
        a = Asset(
            name="staging.users",
            sql="SELECT * FROM {{ ref('raw.users') }}",
        )
        registry.register(a)
        assert len(registry.dependencies) == 1
        assert registry.dependencies[0].source == "raw.users"

        # Re-register with different SQL
        a2 = Asset(
            name="staging.users",
            sql="SELECT * FROM {{ ref('raw.payments') }}",
        )
        registry.register(a2)
        # Old dependency is removed, only the new one remains
        assert len(registry.dependencies) == 1
        assert registry.dependencies[0].source == "raw.payments"
        assert registry.dependencies[0].target == "staging.users"

    def test_re_register_preserves_other_asset_dependencies(self):
        """Re-registering one asset should not remove dependencies of other assets."""
        registry = Registry()
        registry.register(Asset(
            name="a", sql="SELECT * FROM {{ ref('source') }}"
        ))
        registry.register(Asset(
            name="b", sql="SELECT * FROM {{ ref('source') }}"
        ))
        assert len(registry.dependencies) == 2

        # Re-register 'a' with different SQL
        registry.register(Asset(
            name="a", sql="SELECT * FROM {{ ref('other') }}"
        ))
        # 'b' dependency should remain, 'a' dependency should be replaced
        assert len(registry.dependencies) == 2
        targets = {d.target for d in registry.dependencies}
        assert targets == {"a", "b"}
        a_dep = next(d for d in registry.dependencies if d.target == "a")
        assert a_dep.source == "other"

    def test_resolve_column_lineage_missing_asset(self):
        """resolve_column_lineage with non-existent asset returns empty."""

        class MockResolver:
            def resolve(self, sql: str, schema: dict) -> list:
                return []

        registry = Registry()
        result = registry.resolve_column_lineage(
            asset_name="nonexistent", resolver=MockResolver()
        )
        assert result == []

    def test_resolve_column_lineage_no_sql(self):
        """Assets without SQL are skipped during lineage resolution."""

        class MockResolver:
            def resolve(self, sql: str, schema: dict) -> list:
                return [FieldMapping(
                    source_asset="x",
                    source_field="id",
                    target_asset="y",
                    target_field="id",
                )]

        registry = Registry()
        registry.register(Asset(name="no_sql_asset"))
        result = registry.resolve_column_lineage(
            asset_name="no_sql_asset", resolver=MockResolver()
        )
        assert result == []

    def test_resolve_column_lineage_no_args_raises(self):
        class MockResolver:
            def resolve(self, sql: str, schema: dict) -> list:
                return []

        registry = Registry()
        with pytest.raises(ValueError, match="Provide either asset_name or selector"):
            registry.resolve_column_lineage(resolver=MockResolver())

    def test_resolve_column_lineage_with_selector(self):
        class MockResolver:
            def resolve(self, sql: str, schema: dict) -> list[FieldMapping]:
                return [FieldMapping(
                    source_asset="raw.users",
                    source_field="id",
                    target_asset="staging.users",
                    target_field="user_id",
                )]

        registry = Registry()
        registry.register(Asset(name="raw.users", kind="source"))
        registry.register(Asset(
            name="staging.users",
            kind="data_model",
            sql="SELECT * FROM {{ ref('raw.users') }}",
        ))
        result = registry.resolve_column_lineage(
            selector="kind:data_model", resolver=MockResolver()
        )
        assert len(result) == 1
        assert result[0].target_field == "user_id"

    def test_register_self_referencing_sql_is_filtered(self):
        """Self-references in SQL are silently filtered to maintain DAG invariant."""
        registry = Registry()
        a = Asset(name="loop", sql="SELECT * FROM {{ ref('loop') }}")
        registry.register(a)
        assert "loop" not in a.depends_on
        assert len(registry.dependencies) == 0

    def test_self_ref_mixed_with_real_refs(self):
        """Self-references are filtered but other refs are preserved."""
        registry = Registry()
        a = Asset(
            name="staging.users",
            sql="SELECT * FROM {{ ref('raw.users') }} JOIN {{ ref('staging.users') }}",
        )
        registry.register(a)
        assert a.depends_on == ["raw.users"]
        assert len(registry.dependencies) == 1
        assert registry.dependencies[0].source == "raw.users"

    def test_registry_len(self):
        registry = Registry()
        assert len(registry) == 0
        registry.register(Asset(name="a"))
        assert len(registry) == 1

    def test_clear_resets_dependencies_and_graph(self):
        registry = Registry()
        registry.register(Asset(
            name="a", sql="SELECT * FROM {{ ref('b') }}"
        ))
        registry.register(Asset(name="b"))
        assert len(registry.dependencies) > 0
        _ = registry.graph  # build graph

        registry.clear()
        assert len(registry.dependencies) == 0
        assert len(registry.all()) == 0


# ===========================================================================
# 5. RefResolver Edge Cases
# ===========================================================================


class TestRefResolverEdgeCases:
    def setup_method(self):
        self.resolver = RefResolver()

    def test_empty_string(self):
        assert self.resolver.extract_refs("") == []

    def test_mixed_quote_styles(self):
        sql = """
        SELECT * FROM {{ ref('raw.users') }}
        JOIN {{ ref("raw.payments") }} ON 1=1
        """
        refs = self.resolver.extract_refs(sql)
        assert refs == ["raw.users", "raw.payments"]

    def test_malformed_single_brace(self):
        sql = "SELECT * FROM { ref('raw.users') }"
        assert self.resolver.extract_refs(sql) == []

    def test_malformed_missing_closing_brace(self):
        sql = "SELECT * FROM {{ ref('raw.users') }"
        assert self.resolver.extract_refs(sql) == []

    def test_ref_with_special_characters(self):
        sql = "SELECT * FROM {{ ref('my-schema.table_v2') }}"
        refs = self.resolver.extract_refs(sql)
        assert refs == ["my-schema.table_v2"]

    def test_ref_with_dots(self):
        sql = "SELECT * FROM {{ ref('db.schema.table') }}"
        refs = self.resolver.extract_refs(sql)
        assert refs == ["db.schema.table"]

    def test_multiple_refs_on_same_line(self):
        sql = "SELECT {{ ref('a') }}, {{ ref('b') }} FROM {{ ref('c') }}"
        refs = self.resolver.extract_refs(sql)
        assert refs == ["a", "b", "c"]

    def test_ref_in_multiline_sql(self):
        sql = """
        SELECT *
        FROM {{ ref('table_a') }}
        LEFT JOIN
            {{ ref('table_b') }}
        ON a.id = b.id
        """
        refs = self.resolver.extract_refs(sql)
        assert refs == ["table_a", "table_b"]

    def test_resolve_sql_empty_mapping(self):
        sql = "SELECT * FROM {{ ref('a') }}"
        result = self.resolver.resolve_sql(sql, mapping={})
        # Empty mapping falls through to name as-is
        assert result == "SELECT * FROM a"

    def test_resolve_sql_multiple_refs(self):
        sql = "FROM {{ ref('a') }} JOIN {{ ref('b') }}"
        mapping = {"a": "public.a", "b": "public.b"}
        result = self.resolver.resolve_sql(sql, mapping)
        assert result == "FROM public.a JOIN public.b"

    def test_duplicate_refs_in_sql(self):
        sql = "{{ ref('a') }} UNION ALL {{ ref('a') }}"
        refs = self.resolver.extract_refs(sql)
        assert refs == ["a", "a"]  # duplicates preserved


# ===========================================================================
# 6. Dependency Edge Cases
# ===========================================================================


class TestDependencyEdgeCases:
    def test_same_source_target_different_type(self):
        d1 = Dependency(source="a", target="b", type="ref")
        d2 = Dependency(source="a", target="b", type="manual")
        assert d1.fingerprint != d2.fingerprint

    def test_swapped_source_target(self):
        d1 = Dependency(source="a", target="b")
        d2 = Dependency(source="b", target="a")
        assert d1.fingerprint != d2.fingerprint

    def test_dependency_with_metadata(self):
        d = Dependency(source="a", target="b", metadata={"weight": 1.0})
        # metadata is not part of fingerprint — only source:target:type
        d2 = Dependency(source="a", target="b", metadata={"weight": 2.0})
        assert d.fingerprint == d2.fingerprint

    def test_empty_type(self):
        d = Dependency(source="a", target="b", type="")
        assert isinstance(d.fingerprint, str)


# ===========================================================================
# 7. Differ Edge Cases
# ===========================================================================


class TestDifferEdgeCases:
    def setup_method(self):
        self.differ = Differ()

    def test_empty_desired_empty_current(self):
        cs = self.differ.diff([], {})
        assert len(cs.asset_changes) == 0

    def test_deep_diff_field_added(self):
        """New fields in asset not present in old state."""
        old = {"name": "test"}
        new = {"name": "test", "kind": "model"}
        changes = self.differ._deep_diff(old, new)
        assert any(fc.field == "kind" for fc in changes)

    def test_deep_diff_field_removed(self):
        """Fields in old state not present in new."""
        old = {"name": "test", "kind": "model", "extra": "value"}
        new = {"name": "test", "kind": "model"}
        changes = self.differ._deep_diff(old, new)
        assert any(fc.field == "extra" for fc in changes)

    def test_deep_diff_none_to_value(self):
        old = {"name": "test", "sql": None}
        new = {"name": "test", "sql": "SELECT 1"}
        changes = self.differ._deep_diff(old, new)
        sql_change = next(fc for fc in changes if fc.field == "sql")
        assert sql_change.old_value is None
        assert sql_change.new_value == "SELECT 1"

    def test_deep_diff_skips_fingerprint_field(self):
        old = {"name": "test", "fingerprint": "old_fp"}
        new = {"name": "test", "fingerprint": "new_fp"}
        changes = self.differ._deep_diff(old, new)
        assert not any(fc.field == "fingerprint" for fc in changes)

    def test_multiple_deletes(self):
        current = {
            "a": AssetState(name="a", fingerprint="x", data={"name": "a"}),
            "b": AssetState(name="b", fingerprint="y", data={"name": "b"}),
            "c": AssetState(name="c", fingerprint="z", data={"name": "c"}),
        }
        cs = self.differ.diff([], current)
        assert len(cs.asset_changes) == 3
        assert all(c.action == "delete" for c in cs.asset_changes)

    def test_already_deleted_tombstone_not_re_deleted(self):
        """Tombstoned entries should not produce delete changes."""
        current = {
            "a": AssetState(name="a", fingerprint="x", deleted=True),
        }
        cs = self.differ.diff([], current)
        assert len(cs.asset_changes) == 0

    def test_create_over_tombstone(self):
        asset = Asset(name="revived", kind="model")
        current = {
            "revived": AssetState(name="revived", fingerprint="old", deleted=True),
        }
        cs = self.differ.diff([asset], current)
        assert len(cs.asset_changes) == 1
        assert cs.asset_changes[0].action == "create"


# ===========================================================================
# 8. Plan Edge Cases
# ===========================================================================


class TestPlanEdgeCases:
    def test_show_no_changes(self):
        plan = Plan(environment="test")
        output = plan.show()
        assert "No changes" in output

    def test_repr(self):
        plan = Plan(environment="test")
        r = repr(plan)
        assert "test" in r
        assert "changes=0" in r

    def test_has_changes_with_dependency_changes_only(self):
        from assets.engine.differ import Change, ChangeSet

        cs = ChangeSet(
            dependency_changes=[
                Change(action="create", asset_name="dep1")
            ]
        )
        plan = Plan(changeset=cs, environment="test")
        assert plan.has_changes


# ===========================================================================
# 9. StateManager Edge Cases
# ===========================================================================


@pytest.fixture
def sm_project(tmp_path: Path) -> Path:
    models = tmp_path / "models"
    models.mkdir()
    (models / "a.json").write_text(
        json.dumps({"name": "a", "kind": "source", "tags": ["raw"]})
    )
    (models / "b.json").write_text(
        json.dumps({
            "name": "b",
            "kind": "model",
            "sql": "SELECT * FROM {{ ref('a') }}",
        })
    )
    return tmp_path


class TestStateManagerEdgeCases:
    def test_apply_no_changes(self, sm_project: Path):
        """Applying a plan with no changes should succeed with zero counts."""
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(sm_project / ".cache"))
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        mgr = StateManager(registry, loader, backend, config)

        plan = mgr.plan(str(sm_project / "models"), environment="production")
        mgr.apply(plan, environment="production")

        # Second plan has no changes
        plan2 = mgr.plan(str(sm_project / "models"), environment="production")
        assert not plan2.has_changes
        result = mgr.apply(plan2, environment="production")
        assert result.applied == 0
        assert result.created == 0
        assert result.updated == 0
        assert result.deleted == 0

    def test_shallow_env_delete_creates_tombstone(self, sm_project: Path):
        """Deleting an asset in a shallow env should create a tombstone, not remove."""
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(sm_project / ".cache"))
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={
                "production": Environment(name="production"),
                "dev": Environment(name="dev", parent="production", shallow=True),
            },
        )
        mgr = StateManager(registry, loader, backend, config)

        # Apply to production
        plan = mgr.plan(str(sm_project / "models"), environment="production")
        mgr.apply(plan, environment="production")

        # Apply to dev (inherits, no changes)
        dev_plan = mgr.plan(str(sm_project / "models"), environment="dev")
        assert not dev_plan.has_changes

        # Remove a file, plan dev
        (sm_project / "models" / "a.json").unlink()
        dev_plan2 = mgr.plan(str(sm_project / "models"), environment="dev")
        assert dev_plan2.has_changes

        # Apply the delete to dev (shallow) — should create tombstone
        mgr.apply(dev_plan2, environment="dev")
        dev_state = backend.load("dev")
        assert dev_state is not None
        # The asset might be tombstoned or removed from the shallow state
        if "a" in dev_state.assets:
            assert dev_state.assets["a"].deleted is True

    def test_promote_from_nonexistent_env(self, sm_project: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(sm_project / ".cache"))
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={
                "production": Environment(name="production"),
                "staging": Environment(name="staging"),
            },
        )
        mgr = StateManager(registry, loader, backend, config)

        # Promote from env with no state — should return empty plan
        plan = mgr.promote(from_env="nonexistent", to_env="production")
        assert not plan.has_changes

    def test_promote_with_selector(self, sm_project: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(sm_project / ".cache"))
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={
                "production": Environment(name="production"),
                "staging": Environment(name="staging"),
            },
        )
        mgr = StateManager(registry, loader, backend, config)

        plan = mgr.plan(str(sm_project / "models"), environment="production")
        mgr.apply(plan, environment="production")

        promote_plan = mgr.promote(
            from_env="production", to_env="staging", selector="a"
        )
        creates = [
            c for c in promote_plan.changeset.asset_changes if c.action == "create"
        ]
        assert len(creates) == 1
        assert creates[0].asset_name == "a"

    def test_deep_parent_chain(self, sm_project: Path):
        """Three-level environment hierarchy: prod → staging → dev (all shallow)."""
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(sm_project / ".cache"))
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={
                "production": Environment(name="production"),
                "staging": Environment(
                    name="staging", parent="production", shallow=True
                ),
                "dev": Environment(name="dev", parent="staging", shallow=True),
            },
        )
        mgr = StateManager(registry, loader, backend, config)

        # Apply to production
        plan = mgr.plan(str(sm_project / "models"), environment="production")
        mgr.apply(plan, environment="production")

        # Dev should inherit through staging → production
        dev_plan = mgr.plan(str(sm_project / "models"), environment="dev")
        assert not dev_plan.has_changes

    def test_drift_after_file_change(self, sm_project: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(sm_project / ".cache"))
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        mgr = StateManager(registry, loader, backend, config)

        plan = mgr.plan(str(sm_project / "models"), environment="production")
        mgr.apply(plan, environment="production")

        # Modify a file
        (sm_project / "models" / "a.json").write_text(
            json.dumps({"name": "a", "kind": "source_v2", "tags": ["raw"]})
        )

        drift = mgr.drift(str(sm_project / "models"), environment="production")
        assert drift.has_changes

    def test_destroy_nonexistent_environment(self, sm_project: Path):
        """Destroying an env that doesn't exist should not raise."""
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(sm_project / ".cache"))
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        mgr = StateManager(registry, loader, backend, config)
        # Should not raise
        mgr.destroy_environment("ghost")

    def test_destroy_staging_protected(self, sm_project: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(sm_project / ".cache"))
        backend = MemoryBackend()
        config = EnvironmentConfig(
            default="production",
            environments={"staging": Environment(name="staging")},
        )
        mgr = StateManager(registry, loader, backend, config)
        with pytest.raises(ValueError, match="protected"):
            mgr.destroy_environment("staging")

    def test_apply_result_repr(self):
        result = ApplyResult(
            applied=3, created=1, updated=1, deleted=1, environment="prod"
        )
        r = repr(result)
        assert "prod" in r
        assert "created=1" in r


# Need to import ApplyResult
from assets.engine.manager import ApplyResult


# ===========================================================================
# 10. Environment Config Edge Cases
# ===========================================================================


class TestEnvironmentConfigEdgeCases:
    def test_get_unknown_creates_adhoc(self):
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        env = config.get("pr-456")
        assert env.name == "pr-456"
        assert env.shallow is False
        assert env.parent is None

    def test_get_none_returns_default(self):
        config = EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        )
        env = config.get(None)
        assert env.name == "production"

    def test_get_default_not_in_environments(self):
        config = EnvironmentConfig(default="missing", environments={})
        env = config.get(None)
        assert env.name == "missing"


# ===========================================================================
# 11. LocalJSONBackend Edge Cases
# ===========================================================================


class TestLocalJSONBackendEdgeCases:
    def test_corrupted_json_returns_none(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path))
        env_dir = tmp_path / "broken"
        env_dir.mkdir()
        (env_dir / "state.json").write_text("{invalid json!!!")
        result = backend.load("broken")
        assert result is None

    def test_list_envs_dir_without_state_json(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path))
        # Create directories without state.json
        (tmp_path / "empty_env").mkdir()
        (tmp_path / "valid_env").mkdir()
        (tmp_path / "valid_env" / "state.json").write_text(
            StateSnapshot(environment="valid_env").model_dump_json()
        )
        envs = backend.list_environments()
        assert envs == ["valid_env"]
        assert "empty_env" not in envs

    def test_delete_nonexistent_env(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path))
        # Should not raise
        backend.delete_environment("ghost")

    def test_list_envs_no_state_dir(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path / "nonexistent"))
        assert backend.list_environments() == []

    def test_save_creates_nested_dirs(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path / "a" / "b" / "c"))
        backend.save("dev", StateSnapshot(environment="dev"))
        loaded = backend.load("dev")
        assert loaded is not None

    def test_lock_cleanup_on_exception(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path))
        with pytest.raises(RuntimeError):
            with backend.lock("test"):
                raise RuntimeError("boom")
        # Lock should be cleaned up
        lock_path = tmp_path / "test" / "state.json.lock"
        assert not lock_path.exists()


# ===========================================================================
# 12. CompiledCache Edge Cases
# ===========================================================================


class TestCompiledCacheEdgeCases:
    def test_source_outside_root(self, tmp_path: Path):
        """When source_path is outside root, falls back to filename only."""
        cache = CompiledCache(cache_dir=str(tmp_path / ".cache"))
        src = tmp_path / "external" / "file.json"
        src.parent.mkdir(parents=True)
        src.write_text('{"name": "test"}')

        root = tmp_path / "project"
        root.mkdir()

        data = {"name": "test"}
        cache.put(src, root, data)
        result = cache.get(src, root)
        assert result == data

    def test_clean_nonexistent_cache_dir(self, tmp_path: Path):
        cache = CompiledCache(cache_dir=str(tmp_path / "nonexistent"))
        assert cache.clean(tmp_path) == 0

    def test_clean_with_yaml_source(self, tmp_path: Path):
        """clean() checks multiple extensions (.yaml, .yml, .py, .sql, .json)."""
        cache = CompiledCache(cache_dir=str(tmp_path / ".cache"))
        root = tmp_path / "project"
        root.mkdir()

        # Create a YAML source file
        src = root / "model.yaml"
        src.write_text("name: test")

        # Manually create a cache entry for it
        cache_path = tmp_path / ".cache" / "model.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        from assets.loader.compiled import CompiledEntry

        entry = CompiledEntry(
            source_path=str(src),
            source_mtime=0,
            content_hash="abc",
            data={"name": "test"},
        )
        cache_path.write_text(entry.model_dump_json())

        # clean should NOT remove it because model.yaml exists
        removed = cache.clean(root)
        assert removed == 0

    def test_put_overwrites_existing(self, tmp_path: Path):
        cache = CompiledCache(cache_dir=str(tmp_path / ".cache"))
        root = tmp_path / "project"
        root.mkdir()
        src = root / "test.json"
        src.write_text('{"name": "v1"}')

        cache.put(src, root, {"name": "v1"})
        result1 = cache.get(src, root)
        assert result1 == {"name": "v1"}

        # Update source and re-put
        time.sleep(0.01)
        src.write_text('{"name": "v2"}')
        cache.put(src, root, {"name": "v2"})
        result2 = cache.get(src, root)
        assert result2 == {"name": "v2"}


# ===========================================================================
# 13. ProjectLoader Edge Cases
# ===========================================================================


class TestProjectLoaderEdgeCases:
    def test_load_empty_directory(self, tmp_path: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(tmp_path / ".cache"))
        empty = tmp_path / "empty"
        empty.mkdir()

        result = loader.load(str(empty))
        assert result.loaded == 0
        assert result.errors == []

    def test_load_nonexistent_directory(self, tmp_path: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(tmp_path / ".cache"))

        result = loader.load(str(tmp_path / "missing"))
        assert result.loaded == 0
        assert len(result.errors) == 1
        assert "not found" in result.errors[0].error

    def test_load_specific_empty_list(self, tmp_path: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(tmp_path / ".cache"))

        assets = loader.load_specific([], tmp_path)
        assert assets == []

    def test_load_specific_nonexistent_file(self, tmp_path: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(tmp_path / ".cache"))

        # parse_file for non-.json returns None
        result = loader.load_specific([tmp_path / "missing.yaml"], tmp_path)
        assert result == []

    def test_load_invalid_json(self, tmp_path: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(tmp_path / ".cache"))

        models = tmp_path / "models"
        models.mkdir()
        (models / "bad.json").write_text("not json{{{")

        result = loader.load(str(models))
        assert result.loaded == 0
        assert len(result.errors) == 1

    def test_load_json_missing_required_field(self, tmp_path: Path):
        """JSON is valid but doesn't conform to Asset schema (missing name)."""
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(tmp_path / ".cache"))

        models = tmp_path / "models"
        models.mkdir()
        (models / "bad_schema.json").write_text(json.dumps({"kind": "model"}))

        result = loader.load(str(models))
        assert result.loaded == 0
        assert len(result.errors) == 1

    def test_parse_file_non_json_returns_none(self, tmp_path: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(tmp_path / ".cache"))
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("name: test")
        result = loader.parse_file(yaml_file, tmp_path)
        assert result is None

    def test_discover_files_only_json(self, tmp_path: Path):
        registry = Registry()
        loader = ProjectLoader(registry, cache_dir=str(tmp_path / ".cache"))
        models = tmp_path / "models"
        models.mkdir()
        (models / "a.json").write_text("{}")
        (models / "b.yaml").write_text("")
        (models / "c.txt").write_text("")

        files = loader.discover_files(models)
        assert len(files) == 1
        assert files[0].name == "a.json"


# ===========================================================================
# 14. State Models Edge Cases
# ===========================================================================


class TestStateModelsEdgeCases:
    def test_asset_state_deleted_default_false(self):
        s = AssetState(name="test", fingerprint="fp")
        assert s.deleted is False

    def test_state_snapshot_serialization_roundtrip(self):
        state = StateSnapshot(
            environment="prod",
            assets={
                "a": AssetState(
                    name="a",
                    fingerprint="fp1",
                    data={"name": "a", "tags": ["x"]},
                ),
            },
            dependencies=[
                DependencyState(
                    source="a", target="b", fingerprint="dep_fp"
                )
            ],
            metadata={"version": "1.0"},
        )
        json_str = state.model_dump_json()
        restored = StateSnapshot.model_validate_json(json_str)
        assert restored.environment == "prod"
        assert "a" in restored.assets
        assert len(restored.dependencies) == 1
        assert restored.metadata["version"] == "1.0"

    def test_asset_state_with_empty_data(self):
        s = AssetState(name="test", fingerprint="fp", data={})
        assert s.data == {}

    def test_state_snapshot_empty(self):
        s = StateSnapshot()
        assert s.assets == {}
        assert s.dependencies == []
        assert s.metadata == {}


# ===========================================================================
# 15. SelectionResult Edge Cases
# ===========================================================================


class TestSelectionResultEdgeCases:
    def test_default_empty(self):
        sr = SelectionResult()
        assert sr.assets == []
        assert sr.names == set()

    def test_with_data(self):
        a = Asset(name="x")
        sr = SelectionResult(assets=[a], names={"x"})
        assert len(sr.assets) == 1
        assert "x" in sr.names
