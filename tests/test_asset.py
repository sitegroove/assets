"""Tests for the base Asset model."""

from pydantic import BaseModel

from assets import Asset, AssetField


class Column(BaseModel):
    name: str
    type: str = ""
    pii: bool = False


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)
    row_count: int = AssetField(default=0, fingerprint=False)


class TestAsset:
    def test_basic_creation(self):
        a = Asset(name="test")
        assert a.name == "test"
        assert a.kind == ""
        assert a.depends_on == []
        assert a.tags == []
        assert a.sql is None

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

    def test_fingerprint_includes_fingerprinted(self):
        m1 = DataModel(name="test", columns=[Column(name="id")])
        m2 = DataModel(name="test", columns=[Column(name="id"), Column(name="email")])
        assert m1.fingerprint != m2.fingerprint

    def test_canonical_dict_excludes_fingerprint(self):
        a = Asset(name="test")
        d = a._canonical_dict()
        assert "fingerprint" not in d

    def test_canonical_dict_excludes_non_fingerprinted_fields(self):
        m = DataModel(name="test", row_count=42)
        d = m._canonical_dict()
        assert "row_count" not in d

    def test_list_fields_empty(self):
        a = Asset(name="test")
        assert a.list_fields() == []

    def test_list_fields_with_columns(self):
        m = DataModel(
            name="test",
            columns=[Column(name="id"), Column(name="email")],
        )
        assert m.list_fields() == ["id", "email"]

    def test_get_field_found(self):
        m = DataModel(
            name="test",
            columns=[Column(name="id", type="INT"), Column(name="email", type="VARCHAR")],
        )
        f = m.get_field("email")
        assert f is not None
        assert f.type == "VARCHAR"  # type: ignore[attr-defined]

    def test_get_field_not_found(self):
        m = DataModel(name="test", columns=[Column(name="id")])
        assert m.get_field("missing") is None

    def test_get_field_on_base_asset(self):
        a = Asset(name="test")
        assert a.get_field("anything") is None

    def test_model_dump_includes_fingerprint(self):
        a = Asset(name="test")
        d = a.model_dump()
        assert "fingerprint" in d
        assert d["fingerprint"] == a.fingerprint

    def test_subclass_preserves_base_fields(self):
        m = DataModel(name="test", kind="data_model", tags=["staging"])
        assert m.kind == "data_model"
        assert m.tags == ["staging"]
        assert isinstance(m.fingerprint, str)

    def test_cached_fingerprint_bypass(self):
        """When _cached_fingerprint is set, fingerprint returns it directly."""
        a = Asset(name="test")
        real_fp = a.fingerprint
        a._cached_fingerprint = "cached_value"
        assert a.fingerprint == "cached_value"
        # Clear it and we get the real value back
        a._cached_fingerprint = None
        assert a.fingerprint == real_fp

    def test_cached_fingerprint_default_none(self):
        """_cached_fingerprint defaults to None — normal computation occurs."""
        a = Asset(name="test")
        assert a._cached_fingerprint is None
        assert isinstance(a.fingerprint, str)
        assert len(a.fingerprint) == 64  # SHA-256 hex

    def test_model_construct_with_cached_fingerprint(self):
        """model_construct + _cached_fingerprint works for fast cache path."""
        a = Asset.model_construct(name="test", kind="source")
        a._cached_fingerprint = "fast_fp"
        assert a.fingerprint == "fast_fp"
        # model_dump should include the cached fingerprint
        d = a.model_dump()
        assert d["fingerprint"] == "fast_fp"
