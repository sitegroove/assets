"""Tests for the base Asset model."""

from assets import Asset, AssetField


class Column(Asset):
    type: str = ""
    pii: bool = False


class DataModel(Asset):
    row_count: int = AssetField(default=0, fingerprint=False)


class TestAsset:
    def test_new_identity_api(self):
        a = Asset(id="test", type="source")
        assert a.id == "test"
        assert a.type == "source"

    def test_add_dependency_supports_asset_or_str(self):
        source = Asset(id="raw.users")
        model = Asset(id="staging.users")

        model.add_dependency("raw.users")
        model.add_dependency(source)

        assert model.depends_on == ["raw.users"]

    def test_basic_creation(self):
        a = Asset(id="test")
        assert a.id == "test"
        assert a.type == ""
        assert a.depends_on == []
        assert a.tags == []
        assert a.sql is None
        assert a.children == []

    def test_fingerprint_deterministic(self):
        a1 = Asset(id="test", type="source")
        a2 = Asset(id="test", type="source")
        assert a1.fingerprint == a2.fingerprint

    def test_fingerprint_changes_with_content(self):
        a1 = Asset(id="test", type="source")
        a2 = Asset(id="test", type="model")
        assert a1.fingerprint != a2.fingerprint

    def test_fingerprint_excludes_non_fingerprinted(self):
        m1 = DataModel(id="test", row_count=100)
        m2 = DataModel(id="test", row_count=999)
        assert m1.fingerprint == m2.fingerprint

    def test_fingerprint_includes_children(self):
        m1 = Asset(id="test", children=[Asset(id="id")])
        m2 = Asset(id="test", children=[Asset(id="id"), Asset(id="email")])
        assert m1.fingerprint != m2.fingerprint

    def test_canonical_dict_excludes_fingerprint(self):
        a = Asset(id="test")
        d = a._canonical_dict()
        assert "fingerprint" not in d

    def test_canonical_dict_excludes_non_fingerprinted_fields(self):
        m = DataModel(id="test", row_count=42)
        d = m._canonical_dict()
        assert "row_count" not in d

    def test_list_children_empty(self):
        a = Asset(id="test")
        assert a.list_children() == []

    def test_list_children_with_children(self):
        m = Asset(
            id="test",
            children=[Column(id="id"), Column(id="email")],
        )
        assert m.list_children() == ["id", "email"]

    def test_get_child_found(self):
        m = Asset(
            id="test",
            children=[
                Column(id="id", type="INT"),
                Column(id="email", type="VARCHAR"),
            ],
        )
        c = m.get_child("email")
        assert c is not None
        assert c.type == "VARCHAR"

    def test_get_child_not_found(self):
        m = Asset(id="test", children=[Asset(id="id")])
        assert m.get_child("missing") is None

    def test_get_child_on_base_asset(self):
        a = Asset(id="test")
        assert a.get_child("anything") is None

    def test_get_child_at_deep_path(self):
        a = Asset(
            id="database",
            children=[
                Asset(
                    id="public",
                    children=[
                        Asset(
                            id="users",
                            children=[Asset(id="email", type="column")],
                        )
                    ],
                )
            ],
        )
        found = a.get_child_at("public/users/email")
        assert found is not None
        assert found.id == "email"
        assert found.type == "column"

    def test_get_child_at_single_level(self):
        a = Asset(id="table", children=[Asset(id="col1")])
        assert a.get_child_at("col1") is not None

    def test_get_child_at_not_found(self):
        a = Asset(id="table", children=[Asset(id="col1")])
        assert a.get_child_at("col1/subfield") is None

    def test_model_dump_includes_fingerprint(self):
        a = Asset(id="test")
        d = a.model_dump()
        assert "fingerprint" in d
        assert d["fingerprint"] == a.fingerprint

    def test_model_dump_includes_children(self):
        a = Asset(id="test", children=[Asset(id="child1")])
        d = a.model_dump()
        assert "children" in d
        assert len(d["children"]) == 1
        assert d["children"][0]["id"] == "child1"

    def test_model_validate_round_trip(self):
        original = Asset(
            id="test",
            children=[Asset(id="c1", type="column", children=[Asset(id="sub")])],
        )
        dumped = original.model_dump()
        restored = Asset.model_validate(dumped)
        assert restored.id == original.id
        assert restored.children[0].id == "c1"
        assert restored.children[0].children[0].id == "sub"
        assert restored.fingerprint == original.fingerprint

    def test_subclass_preserves_base_fields(self):
        m = DataModel(id="test", type="data_model", tags=["staging"])
        assert m.type == "data_model"
        assert m.tags == ["staging"]
        assert isinstance(m.fingerprint, str)

    def test_children_with_lineage(self):
        child = Asset(id="email_clean", depends_on=["raw.users/email"])
        parent = Asset(id="staging.users", children=[child])
        c = parent.get_child("email_clean")
        assert c is not None
        assert c.depends_on == ["raw.users/email"]

    def test_invalid_id_type_raises(self):
        import pytest

        with pytest.raises(Exception):
            Asset(id=123)  # type: ignore[arg-type]

    def test_very_deep_nesting(self):
        current = Asset(id="leaf", type="column")
        for i in range(9, -1, -1):
            current = Asset(id=f"level_{i}", children=[current])
        node = current
        for i in range(10):
            assert node is not None
            assert len(node.children) == 1
            node = node.children[0]
        assert node.id == "leaf"

    def test_deep_nesting_round_trip(self):
        inner = Asset(id="c", type="column")
        mid = Asset(id="b", children=[inner])
        outer = Asset(id="a", children=[mid])
        dumped = outer.model_dump()
        restored = Asset.model_validate(dumped)
        assert restored.children[0].children[0].id == "c"
        assert restored.fingerprint == outer.fingerprint

    def test_get_child_at_empty_string(self):
        a = Asset(id="test", children=[Asset(id="child")])
        assert a.get_child_at("") is None

    def test_get_child_at_slash_only(self):
        a = Asset(id="test", children=[Asset(id="child")])
        assert a.get_child_at("/") is None

    def test_get_child_at_trailing_slash(self):
        a = Asset(
            id="test",
            children=[Asset(id="child", children=[Asset(id="leaf")])],
        )
        # "child/" splits to ["child", ""] — second part won't match
        assert a.get_child_at("child/") is None
        # But without trailing slash it works
        assert a.get_child_at("child") is not None

    def test_fingerprint_stable_across_calls(self):
        a = Asset(id="test", type="model", tags=["a", "b"])
        fp1 = a.fingerprint
        fp2 = a.fingerprint
        assert fp1 == fp2

    def test_empty_children_list_fingerprint(self):
        a1 = Asset(id="test")
        a2 = Asset(id="test", children=[])
        assert a1.fingerprint == a2.fingerprint
