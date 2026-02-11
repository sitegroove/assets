"""Tests for AssetField metadata."""

from assets.core.fields import (
    FIELD_NAME_KEY,
    FIELD_SOURCE_KEY,
    FINGERPRINT_KEY,
    AssetField,
)


class TestAssetField:
    def test_default_fingerprint_true(self):
        f = AssetField()
        extra = f.json_schema_extra
        assert extra[FINGERPRINT_KEY] is True
        assert extra[FIELD_SOURCE_KEY] is False

    def test_fingerprint_false(self):
        f = AssetField(fingerprint=False)
        extra = f.json_schema_extra
        assert extra[FINGERPRINT_KEY] is False

    def test_field_source_true(self):
        f = AssetField(field_source=True)
        extra = f.json_schema_extra
        assert extra[FIELD_SOURCE_KEY] is True
        assert extra[FIELD_NAME_KEY] == "name"

    def test_custom_field_name_key(self):
        f = AssetField(field_source=True, field_name_key="column_name")
        extra = f.json_schema_extra
        assert extra[FIELD_NAME_KEY] == "column_name"

    def test_default_value(self):
        f = AssetField(default=42)
        assert f.default == 42

    def test_passes_kwargs_through(self):
        f = AssetField(default="x", description="test desc")
        assert f.description == "test desc"

    def test_fingerprint_false_field_source_true(self):
        f = AssetField(fingerprint=False, field_source=True)
        extra = f.json_schema_extra
        assert extra[FINGERPRINT_KEY] is False
        assert extra[FIELD_SOURCE_KEY] is True
