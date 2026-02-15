"""Tests for the base Asset model."""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel

from assets import Asset, AssetField


class Column(Asset):
    type: str = ""
    pii: bool = False


class HasColumns(Asset):
    """Asset subclass with a single children field."""

    columns: list[Column] = cast(
        list[Column],
        AssetField(default_factory=list, children=True),
    )


class Dimension(Asset):
    """Another child type for multi-child-field tests."""

    scope: str = "global"


class MultiChild(Asset):
    """Asset with two distinct children fields."""

    columns: list[Column] = cast(
        list[Column],
        AssetField(default_factory=list, children=True),
    )
    dimensions: list[Dimension] = cast(
        list[Dimension],
        AssetField(default_factory=list, children=True),
    )


class DataModel(Asset):
    row_count: int = cast(int, AssetField(default=0, fingerprint=False))


class PlainModel(BaseModel):
    name: str


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

    def test_base_asset_has_no_children_fields(self):
        """Base Asset has no children field — children() returns []."""
        a = Asset(id="test")
        assert a.children() == []

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
        m1 = HasColumns(id="test", columns=[Column(id="id")])
        m2 = HasColumns(
            id="test",
            columns=[Column(id="id"), Column(id="email")],
        )
        assert m1.fingerprint != m2.fingerprint

    def test_canonical_dict_excludes_fingerprint(self):
        a = Asset(id="test")
        d = a._canonical_dict()
        assert "fingerprint" not in d

    def test_canonical_dict_excludes_non_fingerprinted_fields(self):
        m = DataModel(id="test", row_count=42)
        d = m._canonical_dict()
        assert "row_count" not in d

    # — children() method —

    def test_children_empty(self):
        a = HasColumns(id="test")
        assert a.children() == []

    def test_children_returns_objects(self):
        m = HasColumns(
            id="test",
            columns=[Column(id="id"), Column(id="email")],
        )
        result = m.children()
        assert len(result) == 2
        assert all(isinstance(c, Column) for c in result)
        assert [c.id for c in result] == ["id", "email"]

    def test_children_multi_field(self):
        m = MultiChild(
            id="test",
            columns=[Column(id="id"), Column(id="email")],
            dimensions=[Dimension(id="region")],
        )
        result = m.children()
        assert len(result) == 3
        ids = [c.id for c in result]
        assert ids == ["id", "email", "region"]

    def test_children_filtered_by_type(self):
        m = MultiChild(
            id="test",
            columns=[Column(id="id"), Column(id="email")],
            dimensions=[Dimension(id="region")],
        )
        cols = m.children(child_type=Column)
        assert len(cols) == 2
        assert all(isinstance(c, Column) for c in cols)

        dims = m.children(child_type=Dimension)
        assert len(dims) == 1
        assert isinstance(dims[0], Dimension)
        assert dims[0].id == "region"

    def test_children_filter_no_match(self):
        """Filtering by a type not present returns empty list."""

        class Other(Asset):
            pass

        m = HasColumns(id="test", columns=[Column(id="id")])
        assert m.children(child_type=Other) == []

    # — child() method —

    def test_child_found(self):
        m = HasColumns(
            id="test",
            columns=[
                Column(id="id", type="INT"),
                Column(id="email", type="VARCHAR"),
            ],
        )
        c = m.child("columns/email")
        assert c is not None
        assert isinstance(c, Column)
        assert c.type == "VARCHAR"

    def test_child_not_found(self):
        m = HasColumns(id="test", columns=[Column(id="id")])
        assert m.child("columns/missing") is None

    def test_child_wrong_field(self):
        m = HasColumns(id="test", columns=[Column(id="id")])
        assert m.child("nonexistent/id") is None

    def test_child_no_slash(self):
        """child() requires field/id format."""
        m = HasColumns(id="test", columns=[Column(id="id")])
        assert m.child("id") is None

    def test_child_on_base_asset(self):
        a = Asset(id="test")
        assert a.child("columns/anything") is None

    def test_child_multi_field_disambiguation(self):
        """Same id in different fields — disambiguated by field name."""
        m = MultiChild(
            id="test",
            columns=[Column(id="name", type="VARCHAR")],
            dimensions=[Dimension(id="name", scope="local")],
        )
        col = m.child("columns/name")
        dim = m.child("dimensions/name")
        assert col is not None
        assert dim is not None
        assert isinstance(col, Column)
        assert isinstance(dim, Dimension)
        assert col.type == "VARCHAR"
        assert dim.scope == "local"

    # — child_at() method —

    def test_child_at_single_level(self):
        m = HasColumns(id="test", columns=[Column(id="col1")])
        result = m.child_at("columns/col1")
        assert result is not None
        assert result.id == "col1"

    def test_child_at_deep_path(self):
        """Deep nesting: field/id/field/id pattern."""

        class SubColumn(Asset):
            sub_columns: list[Column] = cast(
                list[Column],
                AssetField(default_factory=list, children=True),
            )

        class HasSubColumns(Asset):
            columns: list[SubColumn] = cast(
                list[SubColumn],
                AssetField(default_factory=list, children=True),
            )

        a = HasSubColumns(
            id="table",
            columns=[
                SubColumn(
                    id="address",
                    sub_columns=[Column(id="city"), Column(id="zip")],
                ),
            ],
        )
        found = a.child_at("columns/address/sub_columns/city")
        assert found is not None
        assert found.id == "city"

    def test_child_at_not_found(self):
        m = HasColumns(id="test", columns=[Column(id="col1")])
        assert m.child_at("columns/col1/sub/missing") is None

    def test_child_at_empty_string(self):
        m = HasColumns(id="test", columns=[Column(id="child")])
        assert m.child_at("") is None

    def test_child_at_single_segment(self):
        """Odd number of segments is invalid."""
        m = HasColumns(id="test", columns=[Column(id="child")])
        assert m.child_at("columns") is None

    def test_child_at_trailing_slash(self):
        m = HasColumns(id="test", columns=[Column(id="child")])
        # "columns/child/" splits to ["columns", "child", ""] — empty segment
        assert m.child_at("columns/child/") is None
        # Without trailing slash works
        assert m.child_at("columns/child") is not None

    # — Serialization —

    def test_model_dump_includes_fingerprint(self):
        a = Asset(id="test")
        d = a.model_dump()
        assert "fingerprint" in d
        assert d["fingerprint"] == a.fingerprint

    def test_model_dump_includes_children_field(self):
        m = HasColumns(id="test", columns=[Column(id="child1")])
        d = m.model_dump()
        assert "columns" in d
        assert len(d["columns"]) == 1
        assert d["columns"][0]["id"] == "child1"

    def test_model_validate_round_trip(self):
        original = HasColumns(
            id="test",
            columns=[Column(id="c1", type="column")],
        )
        dumped = original.model_dump()
        restored = HasColumns.model_validate(dumped)
        assert restored.id == original.id
        assert restored.columns[0].id == "c1"
        assert restored.fingerprint == original.fingerprint

    def test_subclass_preserves_base_fields(self):
        m = DataModel(id="test", type="data_model", tags=["staging"])
        assert m.type == "data_model"
        assert m.tags == ["staging"]
        assert isinstance(m.fingerprint, str)

    def test_children_with_lineage(self):
        child = Column(id="email_clean")
        parent = HasColumns(
            id="staging.users",
            columns=[child],
            depends_on=["raw.users"],
        )
        c = parent.child("columns/email_clean")
        assert c is not None
        assert c.id == "email_clean"

    def test_invalid_id_type_raises(self):
        import pytest

        with pytest.raises(Exception):
            Asset(id=123)  # type: ignore[arg-type]

    def test_very_deep_nesting(self):
        """10-level nesting via a self-referencing child field."""

        class Nested(Asset):
            items: list[Nested] = cast(
                list["Nested"],
                AssetField(default_factory=list, children=True),
            )

        Nested.model_rebuild()

        current = Nested(id="leaf", type="column")
        for i in range(9, -1, -1):
            current = Nested(id=f"level_{i}", items=[current])
        node: Asset | None = current
        for i in range(10):
            assert node is not None
            kids = node.children()
            assert len(kids) == 1
            node = kids[0]
        assert node is not None
        assert node.id == "leaf"

    def test_deep_nesting_round_trip(self):
        class Nested(Asset):
            items: list[Nested] = cast(
                list["Nested"],
                AssetField(default_factory=list, children=True),
            )

        Nested.model_rebuild()

        inner = Nested(id="c", type="column")
        mid = Nested(id="b", items=[inner])
        outer = Nested(id="a", items=[mid])
        dumped = outer.model_dump()
        restored = Nested.model_validate(dumped)
        assert restored.items[0].items[0].id == "c"
        assert restored.fingerprint == outer.fingerprint

    def test_fingerprint_stable_across_calls(self):
        a = Asset(id="test", type="model", tags=["a", "b"])
        fp1 = a.fingerprint
        fp2 = a.fingerprint
        assert fp1 == fp2

    def test_empty_children_list_fingerprint(self):
        a1 = HasColumns(id="test")
        a2 = HasColumns(id="test", columns=[])
        assert a1.fingerprint == a2.fingerprint

    def test_add_dependency_empty_string_raises(self):
        import pytest

        a = Asset(id="x")
        with pytest.raises(ValueError, match="non-empty"):
            a.add_dependency("   ")

    def test_hash_and_eq_behaviour(self):
        a1 = Asset(id="same")
        a2 = Asset(id="same")
        a3 = Asset(id="different")

        assert hash(a1) == hash("same")
        assert a1 == a2
        assert a1 != a3
        assert a1.__eq__(123) is NotImplemented

    def test_repr_includes_type_and_children_count(self):
        a = HasColumns(
            id="users",
            type="source",
            columns=[Column(id="id")],
        )
        rendered = repr(a)

        assert rendered.startswith("Asset(")
        assert "id='users'" in rendered
        assert "type='source'" in rendered
        assert "children=1" in rendered

    def test_repr_no_children_on_base(self):
        a = Asset(id="users", type="source")
        rendered = repr(a)
        assert "children" not in rendered

    def test_canonical_dict_serializes_basemodel_without_canonical_dict(self):
        a = Asset(id="test", metadata={"owner": PlainModel(name="data")})
        payload = a._canonical_dict()

        assert payload["metadata"]["owner"] == {"name": "data"}

    def test_canonical_dict_serializes_set_as_sorted_list(self):
        a = Asset(id="test", metadata={"letters": {"b", "a"}})
        payload = a._canonical_dict()

        assert payload["metadata"]["letters"] == ["a", "b"]

    def test_child_fields_introspection(self):
        """_child_fields returns dict of field_name -> list."""
        m = MultiChild(
            id="test",
            columns=[Column(id="a")],
            dimensions=[Dimension(id="b"), Dimension(id="c")],
        )
        fields = m._child_fields()
        assert set(fields.keys()) == {"columns", "dimensions"}
        assert len(fields["columns"]) == 1
        assert len(fields["dimensions"]) == 2
