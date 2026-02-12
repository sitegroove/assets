"""Tests for the base Asset model."""

from assets import Asset, AssetField


class Column(Asset):
    type: str = ""
    pii: bool = False


class DataModel(Asset):
    row_count: int = AssetField(default=0, fingerprint=False)


class TestAsset:
    def test_basic_creation(self):
        a = Asset(name="test")
        assert a.name == "test"
        assert a.kind == ""
        assert a.depends_on == []
        assert a.tags == []
        assert a.sql is None
        assert a.children == []

    def test_fingerprint_deterministic(self):
        a1 = Asset(name="test", kind="source")
        a2 = Asset(name="test", kind="source")
        assert a1.fingerprint == a2.fingerprint

    def test_fingerprint_changes_with_content(self):
        a1 = Asset(name="test", kind="source")
        a2 = Asset(name="test", kind="model")
        assert a1.fingerprint != a2.fingerprint

    def test_fingerprint_excludes_non_fingerprinted(self):
        m1 = DataModel(name="test", row_count=100)
        m2 = DataModel(name="test", row_count=999)
        assert m1.fingerprint == m2.fingerprint

    def test_fingerprint_includes_children(self):
        m1 = Asset(name="test", children=[Asset(name="id")])
        m2 = Asset(name="test", children=[Asset(name="id"), Asset(name="email")])
        assert m1.fingerprint != m2.fingerprint

    def test_canonical_dict_excludes_fingerprint(self):
        a = Asset(name="test")
        d = a._canonical_dict()
        assert "fingerprint" not in d

    def test_canonical_dict_excludes_non_fingerprinted_fields(self):
        m = DataModel(name="test", row_count=42)
        d = m._canonical_dict()
        assert "row_count" not in d

    def test_list_children_empty(self):
        a = Asset(name="test")
        assert a.list_children() == []

    def test_list_children_with_children(self):
        m = Asset(
            name="test",
            children=[Column(name="id"), Column(name="email")],
        )
        assert m.list_children() == ["id", "email"]

    def test_get_child_found(self):
        m = Asset(
            name="test",
            children=[
                Column(name="id", type="INT"),
                Column(name="email", type="VARCHAR"),
            ],
        )
        c = m.get_child("email")
        assert c is not None
        assert c.type == "VARCHAR"  # type: ignore[attr-defined]

    def test_get_child_not_found(self):
        m = Asset(name="test", children=[Asset(name="id")])
        assert m.get_child("missing") is None

    def test_get_child_on_base_asset(self):
        a = Asset(name="test")
        assert a.get_child("anything") is None

    def test_get_child_at_deep_path(self):
        a = Asset(
            name="database",
            children=[
                Asset(
                    name="public",
                    children=[
                        Asset(
                            name="users",
                            children=[Asset(name="email", kind="column")],
                        )
                    ],
                )
            ],
        )
        found = a.get_child_at("public/users/email")
        assert found is not None
        assert found.name == "email"
        assert found.kind == "column"

    def test_get_child_at_single_level(self):
        a = Asset(name="table", children=[Asset(name="col1")])
        assert a.get_child_at("col1") is not None

    def test_get_child_at_not_found(self):
        a = Asset(name="table", children=[Asset(name="col1")])
        assert a.get_child_at("col1/subfield") is None

    def test_model_dump_includes_fingerprint(self):
        a = Asset(name="test")
        d = a.model_dump()
        assert "fingerprint" in d
        assert d["fingerprint"] == a.fingerprint

    def test_model_dump_includes_children(self):
        a = Asset(name="test", children=[Asset(name="child1")])
        d = a.model_dump()
        assert "children" in d
        assert len(d["children"]) == 1
        assert d["children"][0]["name"] == "child1"

    def test_model_validate_round_trip(self):
        original = Asset(
            name="test",
            children=[Asset(name="c1", kind="column", children=[Asset(name="sub")])],
        )
        dumped = original.model_dump()
        restored = Asset.model_validate(dumped)
        assert restored.name == original.name
        assert restored.children[0].name == "c1"
        assert restored.children[0].children[0].name == "sub"
        assert restored.fingerprint == original.fingerprint

    def test_subclass_preserves_base_fields(self):
        m = DataModel(name="test", kind="data_model", tags=["staging"])
        assert m.kind == "data_model"
        assert m.tags == ["staging"]
        assert isinstance(m.fingerprint, str)

    def test_children_with_lineage(self):
        child = Asset(name="email_clean", depends_on=["raw.users/email"])
        parent = Asset(name="staging.users", children=[child])
        c = parent.get_child("email_clean")
        assert c is not None
        assert c.depends_on == ["raw.users/email"]

    def test_invalid_name_type_raises(self):
        import pytest

        with pytest.raises(Exception):
            Asset(name=123)  # type: ignore[arg-type]

    def test_very_deep_nesting(self):
        current = Asset(name="leaf", kind="column")
        for i in range(9, -1, -1):
            current = Asset(name=f"level_{i}", children=[current])
        node = current
        for i in range(10):
            assert node is not None
            assert len(node.children) == 1
            node = node.children[0]
        assert node.name == "leaf"

    def test_deep_nesting_round_trip(self):
        inner = Asset(name="c", kind="column")
        mid = Asset(name="b", children=[inner])
        outer = Asset(name="a", children=[mid])
        dumped = outer.model_dump()
        restored = Asset.model_validate(dumped)
        assert restored.children[0].children[0].name == "c"
        assert restored.fingerprint == outer.fingerprint

    def test_get_child_at_empty_string(self):
        a = Asset(name="test", children=[Asset(name="child")])
        assert a.get_child_at("") is None

    def test_get_child_at_slash_only(self):
        a = Asset(name="test", children=[Asset(name="child")])
        assert a.get_child_at("/") is None

    def test_get_child_at_trailing_slash(self):
        a = Asset(
            name="test",
            children=[Asset(name="child", children=[Asset(name="leaf")])],
        )
        # "child/" splits to ["child", ""] — second part won't match
        assert a.get_child_at("child/") is None
        # But without trailing slash it works
        assert a.get_child_at("child") is not None

    def test_fingerprint_stable_across_calls(self):
        a = Asset(name="test", kind="model", tags=["a", "b"])
        fp1 = a.fingerprint
        fp2 = a.fingerprint
        assert fp1 == fp2

    def test_empty_children_list_fingerprint(self):
        a1 = Asset(name="test")
        a2 = Asset(name="test", children=[])
        assert a1.fingerprint == a2.fingerprint
